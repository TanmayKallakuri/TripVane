from tripvane_core.domains import extract_domains, extract_domains_from_value, normalize_domain


def test_extracts_hosts_from_urls_and_email_addresses() -> None:
    text = (
        "Fetch https://Exfil.Example.COM/collect?k=1 then mail it to drop@mailbox.example.net "
        "or post to http://user:pw@upload.example.org:8443/x"
    )
    assert extract_domains(text) == {
        "exfil.example.com",
        "mailbox.example.net",
        "upload.example.org",
    }


def test_ignores_bare_filenames_ip_addresses_and_single_labels() -> None:
    text = "read config.yaml and http://203.0.113.9/payload and http://localhost:8080/"
    assert extract_domains(text) == set()


def test_normalize_domain() -> None:
    assert normalize_domain("Example.COM.") == "example.com"
    assert normalize_domain("bücher.example") == "xn--bcher-kva.example"
    assert normalize_domain("-bad.example") is None
    assert normalize_domain("10.0.0.1") is None
    assert normalize_domain("a" * 64 + ".example") is None


def test_extracts_from_nested_tool_arguments() -> None:
    arguments = {
        "to": "ops@corp-backup.example",
        "headers": [{"Referer": "https://cdn.example.io/a.js"}],
        "retries": 3,
    }
    assert extract_domains_from_value(arguments) == {"corp-backup.example", "cdn.example.io"}
