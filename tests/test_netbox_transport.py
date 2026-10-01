"""What the client does when the thing answering is not NetBox.

Written after a production run hit an AWS ALB with OIDC in front of NetBox: the
API token never reached the application, ``requests`` followed the redirect to
the identity provider, and the client tried to parse a login page as a device
list. Every case here previously produced a traceback rather than a sentence.
"""

from __future__ import annotations

import pytest
import responses

from nbrecon.collect.netbox import NetBoxClient
from nbrecon.config import NetBoxSettings
from nbrecon.errors import ApplyError, CollectionError

BASE = "https://netbox.example"
STATUS = f"{BASE}/api/status/"
IDP = "https://login.microsoftonline.com/oauth2/authorize"

LOGIN_PAGE = "<html>\n<head><title>Sign in</title></head>\n<body>Sign in</body>\n</html>"


@pytest.fixture
def client() -> NetBoxClient:
    return NetBoxClient(NetBoxSettings(url=BASE, token="t"))


# --- a proxy standing in front of the API ----------------------------------


@responses.activate
def test_a_redirect_to_another_host_is_refused(client):
    responses.add(responses.GET, STATUS, status=302, headers={"Location": IDP})
    responses.add(responses.GET, IDP, body=LOGIN_PAGE, content_type="text/html")

    with pytest.raises(CollectionError) as exc:
        client.check()

    message = str(exc.value)
    assert "redirected to login.microsoftonline.com" in message
    assert "token never reaches NetBox" in message


@responses.activate
def test_a_same_host_redirect_is_still_followed(client):
    """NetBox appends a trailing slash; that is not a proxy."""
    responses.add(
        responses.GET, f"{BASE}/api/status", status=301, headers={"Location": STATUS}
    )
    responses.add(responses.GET, STATUS, json={"netbox-version": "4.1.0"})

    assert client._get("api/status")["netbox-version"] == "4.1.0"


@responses.activate
def test_html_with_a_200_is_named_not_parsed(client):
    responses.add(responses.GET, STATUS, body=LOGIN_PAGE, content_type="text/html")

    with pytest.raises(CollectionError) as exc:
        client.check()

    message = str(exc.value)
    assert "not JSON" in message
    assert "text/html" in message
    # The body is quoted so the operator can recognise their own login page.
    assert "Sign in" in message


@responses.activate
def test_an_empty_body_does_not_produce_a_traceback(client):
    responses.add(responses.GET, STATUS, body="", content_type="text/plain")

    with pytest.raises(CollectionError, match="not JSON"):
        client.check()


@responses.activate
def test_json_that_is_not_an_object_is_refused(client):
    responses.add(responses.GET, STATUS, json=["not", "a", "netbox", "response"])

    with pytest.raises(CollectionError, match="not an object"):
        client.check()


@responses.activate
def test_an_http_error_still_reports_its_status(client):
    """The pre-existing 4xx path must keep working."""
    responses.add(responses.GET, STATUS, status=403, json={"detail": "no"})

    with pytest.raises(CollectionError, match="403"):
        client.check()


# --- pagination ------------------------------------------------------------


@responses.activate
def test_a_proxy_intercepting_page_two_is_caught(client):
    """The second page is fetched by a separate call that had the same hole."""
    first = {"count": 2, "next": f"{BASE}/api/dcim/racks/?page=2", "results": [{"id": 1}]}
    responses.add(responses.GET, f"{BASE}/api/dcim/racks/", json=first)
    responses.add(
        responses.GET,
        f"{BASE}/api/dcim/racks/?page=2",
        status=302,
        headers={"Location": IDP},
    )
    responses.add(responses.GET, IDP, body=LOGIN_PAGE, content_type="text/html")

    with pytest.raises(CollectionError, match="redirected to"):
        client._paginate("api/dcim/racks/", {})


# --- writes ----------------------------------------------------------------


@responses.activate
def test_a_redirected_patch_says_no_change_was_made(client):
    """The dangerous case: a 302 turns a PATCH into a GET, so nothing is written."""
    url = f"{BASE}/api/dcim/devices/1/"
    responses.add(responses.PATCH, url, status=302, headers={"Location": IDP})
    responses.add(responses.GET, IDP, body=LOGIN_PAGE, content_type="text/html")

    with pytest.raises(ApplyError) as exc:
        client.patch_device(1, {"serial": "SN-1"})

    assert "no change was made" in str(exc.value)


@responses.activate
def test_a_patch_answered_with_html_is_not_treated_as_success(client):
    url = f"{BASE}/api/dcim/devices/1/"
    responses.add(responses.PATCH, url, body=LOGIN_PAGE, content_type="text/html")

    with pytest.raises(ApplyError, match="not JSON"):
        client.patch_device(1, {"serial": "SN-1"})


@responses.activate
def test_a_journal_entry_answered_with_html_is_refused(client):
    responses.add(
        responses.POST,
        f"{BASE}/api/extras/journal-entries/",
        body=LOGIN_PAGE,
        content_type="text/html",
    )

    with pytest.raises(ApplyError, match="not JSON"):
        client.create_journal_entry(1, "note")


@responses.activate
def test_a_successful_patch_is_unaffected(client):
    url = f"{BASE}/api/dcim/devices/1/"
    responses.add(responses.PATCH, url, json={"id": 1, "serial": "SN-1"})

    assert client.patch_device(1, {"serial": "SN-1"})["serial"] == "SN-1"
