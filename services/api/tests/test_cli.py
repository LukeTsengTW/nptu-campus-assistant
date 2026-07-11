from nptu_assistant.cli import build_parser


def test_cli_exposes_required_commands() -> None:
    parser = build_parser()

    for command in ("seed", "ingest-documents", "crawl-announcements", "export-openapi"):
        assert parser.parse_args([command]).command == command
