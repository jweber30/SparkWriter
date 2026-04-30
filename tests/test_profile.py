import json

from spark_writer.profile import ProfileStore


def test_profile_store_round_trips_standard_field_values(tmp_path):
    store = ProfileStore(tmp_path / "profile.json")

    store.save_values(
        {
            "user.ssh_public_keys": "ssh-ed25519 AAAA demo",
            "network.wifi.ssid": "Lab",
        }
    )

    values = store.load_values()
    assert values["user.ssh_public_keys"] == "ssh-ed25519 AAAA demo"
    assert values["network.wifi.ssid"] == "Lab"


def test_profile_store_accepts_legacy_flat_payload(tmp_path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps({"network.hostname": "sparkbox"}),
        encoding="utf-8",
    )

    assert ProfileStore(profile_path).load_values()["network.hostname"] == "sparkbox"
