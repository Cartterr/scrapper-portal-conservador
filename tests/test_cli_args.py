from cbrs.cli import _runtime_headless, build_parser, main, missing_fna_fields


def test_fna_requires_numero_and_ano() -> None:
    parser = build_parser()
    args = parser.parse_args(["search", "--foja", "123"])

    assert missing_fna_fields(args) == ["numero", "ano"]


def test_complete_fna_has_no_missing_fields() -> None:
    parser = build_parser()
    args = parser.parse_args(["download", "--foja", "123", "--numero", "456", "--ano", "2024"])

    assert missing_fna_fields(args) == []


def test_headed_overrides_default_headless() -> None:
    parser = build_parser()
    args = parser.parse_args(["--headed", "search", "--query", "BANCO DE CHILE"])

    assert _runtime_headless(args) is False


def test_headless_flag_enables_headless() -> None:
    parser = build_parser()
    args = parser.parse_args(["--headless", "search", "--query", "BANCO DE CHILE"])

    assert _runtime_headless(args) is True


def test_headless_and_headed_are_mutually_exclusive(capsys) -> None:
    try:
        main(["--headless", "--headed", "doctor"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("Expected conflicting headless flags to fail")

    assert "--headless and --headed cannot be used together" in capsys.readouterr().err


def test_soak_run_parser_supports_dashboard_dry_run() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["soak", "run", "--dashboard", "--dry-run", "--max-cycles", "3"]
    )

    assert args.command == "soak"
    assert args.soak_command == "run"
    assert args.dashboard is True
    assert args.dry_run is True
    assert args.max_cycles == 3


def test_soak_dashboard_parser_is_independent_from_run() -> None:
    parser = build_parser()
    args = parser.parse_args(["soak", "dashboard", "--port", "9876"])

    assert args.command == "soak"
    assert args.soak_command == "dashboard"
    assert args.port == 9876


def test_soak_stop_parser() -> None:
    parser = build_parser()
    args = parser.parse_args(["soak", "stop"])

    assert args.command == "soak"
    assert args.soak_command == "stop"
