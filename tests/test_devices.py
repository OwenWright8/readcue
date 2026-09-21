"""Choosing which Pushover devices get notifications."""

import json
from urllib.parse import parse_qs

import pytest
from conftest import FakeNotifier

from readcue.config import Config
from readcue.errors import NotifyError
from readcue.notify import DEVICES_SETTING, DEVICES_URL, PushoverNotifier, notifier_for
from readcue.web import create_app


class Pushover:
    """A stand-in for api.pushover.net that records what was posted."""

    def __init__(self, devices=("iphone", "ipad", "work-mac"), status=200, body=None):
        self.devices = list(devices)
        self.calls: list[tuple[str, dict]] = []
        self.status, self.body = status, body

    def __call__(self, url, data, headers, timeout):
        form = {k: v[0] for k, v in parse_qs(data.decode()).items()}
        self.calls.append((url, form))
        if self.body is not None:
            return self.status, json.dumps(self.body).encode()
        if url == DEVICES_URL:
            return 200, json.dumps({"status": 1, "devices": self.devices}).encode()
        return 200, json.dumps({"status": 1}).encode()

    @property
    def sent(self) -> list[dict]:
        return [form for url, form in self.calls if url != DEVICES_URL]


def notifier(cfg, pushover, *, override=None):
    return PushoverNotifier(cfg, transport=pushover, device_override=lambda: override)


# ---- which devices a notification goes to -------------------------------------------------


def test_no_device_choice_means_every_device(cfg):
    pushover = Pushover()
    notifier(cfg, pushover).send("t", "m")
    assert "device" not in pushover.sent[0]


def test_the_environment_setting_is_the_default(cfg):
    cfg.pushover_device = "iphone"
    pushover = Pushover()
    n = notifier(cfg, pushover)
    n.send("t", "m")
    assert pushover.sent[0]["device"] == "iphone" and n.selected_devices == ["iphone"]


def test_a_choice_made_in_the_ui_overrides_the_environment(cfg):
    cfg.pushover_device = "iphone"
    pushover = Pushover()
    notifier(cfg, pushover, override="ipad").send("t", "m")
    assert pushover.sent[0]["device"] == "ipad"


def test_choosing_all_devices_in_the_ui_overrides_an_environment_device(cfg):
    cfg.pushover_device = "iphone"
    pushover = Pushover()
    n = notifier(cfg, pushover, override="")  # saved as "all devices"
    n.send("t", "m")
    assert "device" not in pushover.sent[0] and n.selected_devices == []


def test_several_devices_are_sent_comma_separated(cfg):
    pushover = Pushover()
    n = notifier(cfg, pushover, override=" iphone , work-mac ,")
    n.send("t", "m")
    assert pushover.sent[0]["device"] == "iphone,work-mac" and n.selected_devices == ["iphone", "work-mac"]


def test_the_choice_is_read_fresh_each_time(cfg, db):
    pushover = Pushover()
    n = PushoverNotifier(cfg, transport=pushover, device_override=lambda: db.get_setting(DEVICES_SETTING))
    n.send("a", "m")
    db.set_setting(DEVICES_SETTING, "ipad")
    n.send("b", "m")
    assert ["device" in s for s in pushover.sent] == [False, True] and pushover.sent[1]["device"] == "ipad"


def test_notifier_for_uses_the_saved_choice(cfg, db):
    db.set_setting(DEVICES_SETTING, "work-mac")
    assert notifier_for(cfg, db).selected_devices == ["work-mac"]


# ---- listing the account's devices --------------------------------------------------------


def test_lists_the_devices_on_the_account(cfg):
    pushover = Pushover(devices=["iphone", "ipad"])
    assert notifier(cfg, pushover).list_devices() == ["iphone", "ipad"]
    url, form = pushover.calls[0]
    assert url == DEVICES_URL and form == {"token": "tok", "user": "usr"}


def test_listing_devices_reports_pushovers_complaint(cfg):
    pushover = Pushover(status=400, body={"status": 0, "errors": ["user identifier is invalid"]})
    with pytest.raises(NotifyError, match="user identifier is invalid"):
        notifier(cfg, pushover).list_devices()


def test_listing_devices_needs_pushover_configured():
    with pytest.raises(NotifyError, match="isn't configured"):
        PushoverNotifier(Config()).list_devices()


def test_listing_devices_survives_a_network_failure(cfg):
    def down(url, data, headers, timeout):
        raise OSError("no route to host")

    with pytest.raises(NotifyError, match="Couldn't reach Pushover"):
        PushoverNotifier(cfg, transport=down).list_devices()


# ---- saved settings -----------------------------------------------------------------------


def test_settings_distinguish_never_set_from_set_to_empty(db):
    assert db.get_setting("x") is None
    db.set_setting("x", "")
    assert db.get_setting("x") == ""
    db.set_setting("x", "a,b")
    assert db.get_setting("x") == "a,b"


# ---- the pages ----------------------------------------------------------------------------


@pytest.fixture
def app_parts(cfg, db):
    pushover = Pushover()
    real = PushoverNotifier(cfg, transport=pushover, device_override=lambda: db.get_setting(DEVICES_SETTING))
    client = create_app(cfg, db, notifier=real).test_client()
    return client, pushover, db


def test_the_picker_lists_the_devices_and_checks_the_current_choice(app_parts):
    client, _, db = app_parts
    page = client.get("/settings/devices").data.decode()
    for name in ("iphone", "ipad", "work-mac"):
        assert f'value="{name}"' in page
    assert 'value="all" checked' in page and "checked>" not in page.split('name="device"')[1]

    db.set_setting(DEVICES_SETTING, "ipad")
    page = client.get("/settings/devices").data.decode()
    assert 'value="some" id="mode-some" checked' in page
    assert 'value="ipad" checked' in page and 'value="iphone" checked' not in page


def test_saving_specific_devices(app_parts):
    client, pushover, db = app_parts
    resp = client.post(
        "/settings/devices", data={"mode": "some", "device": ["iphone", "work-mac"]}, follow_redirects=True
    )
    assert db.get_setting(DEVICES_SETTING) == "iphone,work-mac"
    assert b"Notifications will go to iphone, work-mac" in resp.data
    assert b"iphone, work-mac" in client.get("/settings").data
    assert pushover.sent == []  # saving alone doesn't notify


def test_saving_all_devices(app_parts):
    client, _, db = app_parts
    db.set_setting(DEVICES_SETTING, "ipad")
    resp = client.post("/settings/devices", data={"mode": "all", "device": ["ipad"]}, follow_redirects=True)
    assert db.get_setting(DEVICES_SETTING) == "" and b"all your devices" in resp.data
    assert b"All devices" in client.get("/settings").data


def test_save_and_send_a_test_goes_to_the_chosen_devices(app_parts):
    client, pushover, db = app_parts
    resp = client.post(
        "/settings/devices", data={"mode": "some", "device": ["ipad"], "test": "1"}, follow_redirects=True
    )
    assert b"A test notification went to ipad" in resp.data
    assert pushover.sent[-1]["device"] == "ipad" and pushover.sent[-1]["title"] == "readcue test"


def test_the_regular_test_button_also_honours_the_choice(app_parts):
    client, pushover, db = app_parts
    db.set_setting(DEVICES_SETTING, "iphone")
    client.post("/settings/test-push")
    assert pushover.sent[-1]["device"] == "iphone"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"mode": "some"}, b"Tick at least one device"),
        ({"mode": "some", "device": ["nokia"]}, b"no device called nokia"),
    ],
)
def test_bad_choices_are_rejected_and_change_nothing(app_parts, data, message):
    client, _, db = app_parts
    db.set_setting(DEVICES_SETTING, "ipad")
    assert message in client.post("/settings/devices", data=data, follow_redirects=True).data
    assert db.get_setting(DEVICES_SETTING) == "ipad"


def test_a_selected_device_that_no_longer_exists_is_flagged(app_parts):
    client, _, db = app_parts
    db.set_setting(DEVICES_SETTING, "old-phone")
    assert b"old-phone" in client.get("/settings/devices").data
    assert b"isn't on your Pushover account any more" in client.get("/settings/devices").data.replace(
        b"&#39;", b"'"
    )


def test_the_picker_explains_when_pushover_cant_be_reached(cfg, db):
    def down(url, data, headers, timeout):
        raise OSError("network is unreachable")

    client = create_app(cfg, db, notifier=PushoverNotifier(cfg, transport=down)).test_client()
    page = client.get("/settings/devices").data.decode()
    assert "Couldn't load your devices" in page and "network is unreachable" in page and "Try again" in page


def test_the_picker_explains_when_pushover_isnt_set_up(db, cfg):
    cfg.pushover_app_token = cfg.pushover_user_key = ""
    client = create_app(cfg, db, notifier=PushoverNotifier(cfg)).test_client()
    assert b"Pushover isn" in client.get("/settings/devices").data
    assert b"Choose devices" not in client.get("/settings").data  # no link until it's configured


def test_settings_offers_the_picker_when_configured(cfg, db, notifier):
    page = create_app(cfg, db, notifier=notifier).test_client().get("/settings").data
    assert b"Choose devices" in page and b"All devices" in page
    assert isinstance(notifier, FakeNotifier)
