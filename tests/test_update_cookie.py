from pathlib import Path

from update_cookie import extract_cookie_strings, normalize_cookie_accounts, patch_runtime_config


def test_normalize_cookie_accounts_extracts_required_fields():
    accounts = normalize_cookie_accounts(
        [
            "a=b; __Secure-1PSID=psid_1; __Secure-1PSIDTS=psidts_1;",
            "a=b; __Secure-1PSID=psid_2; __Secure-1PSIDTS=psidts_2;",
        ]
    )
    assert accounts["1"]["SECURE_1PSID"] == "psid_1"
    assert accounts["1"]["SECURE_1PSIDTS"] == "psidts_1"
    assert accounts["2"]["SECURE_1PSID"] == "psid_2"


def test_extract_cookie_strings_accepts_template_list_items():
    cookies = extract_cookie_strings(
        [
            {"__template_key": "cookie_account", "cookie": "a=b; __Secure-1PSID=psid_1;"},
            "a=b; __Secure-1PSID=psid_2;",
        ]
    )
    assert cookies == [
        "a=b; __Secure-1PSID=psid_1;",
        "a=b; __Secure-1PSID=psid_2;",
    ]


def test_patch_runtime_config_writes_latest_accounts(tmp_path: Path):
    runtime_path = tmp_path / "runtime_config.json"
    patch_runtime_config(
        runtime_path,
        ["a=b; __Secure-1PSID=psid_1; __Secure-1PSIDTS=psidts_1;"],
    )
    content = runtime_path.read_text(encoding="utf-8")
    assert "psid_1" in content
    assert "psidts_1" in content


def test_patch_runtime_config_preserves_existing_non_account_fields(tmp_path: Path):
    runtime_path = tmp_path / "runtime_config.json"
    runtime_path.write_text(
        '{"proxy":"http://127.0.0.1:7890","cookie_accounts":["raw_cookie"]}',
        encoding="utf-8",
    )

    patch_runtime_config(
        runtime_path,
        ["a=b; __Secure-1PSID=psid_1; __Secure-1PSIDTS=psidts_1;"],
    )

    content = runtime_path.read_text(encoding="utf-8")
    assert '"proxy": "http://127.0.0.1:7890"' in content
    assert '"cookie_accounts"' in content
    assert '"accounts"' in content
