from nptu_assistant.cli import build_parser
from nptu_assistant import cli


def test_cli_exposes_required_commands() -> None:
    parser = build_parser()

    for command in (
        "seed",
        "ingest-documents",
        "crawl-announcements",
        "export-openapi",
    ):
        assert parser.parse_args([command]).command == command


def test_cli_stop_handlers_forward_signal_and_restore(monkeypatch) -> None:
    installed = {}
    restored = []

    def fake_signal(signum, handler):
        installed[signum] = handler
        restored.append((signum, handler))

    monkeypatch.setattr(cli.signal, "getsignal", lambda _signum: "previous")
    monkeypatch.setattr(cli.signal, "signal", fake_signal)
    stopped = []

    with cli._install_stop_handlers(lambda: stopped.append(True)):
        installed[cli.signal.SIGINT](cli.signal.SIGINT, None)

    assert stopped == [True]
    assert (cli.signal.SIGINT, "previous") in restored
