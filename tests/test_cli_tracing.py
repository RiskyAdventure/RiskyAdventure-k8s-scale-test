"""Unit tests for CLI tracing integration (Task 2.4).

Tests that the --enable-tracing flag is parsed correctly, tracing is
skipped when the flag is absent, and the CLI handles init_tracing
returning False gracefully.

Requirements: 1.1, 1.2, 1.6
"""

from unittest.mock import patch, MagicMock

from k8s_scale_test.cli import parse_args


class TestEnableTracingFlagParsing:
    """Verify --enable-tracing flag is parsed correctly."""

    def test_flag_present(self):
        args = parse_args(["run", "--target-pods", "100", "--enable-tracing"])
        assert args.enable_tracing is True

    def test_flag_absent(self):
        args = parse_args(["run", "--target-pods", "100"])
        assert args.enable_tracing is False

    def test_legacy_invocation_with_flag(self):
        """Legacy mode (no 'run' subcommand) also supports the flag."""
        args = parse_args(["--target-pods", "100", "--enable-tracing"])
        assert args.enable_tracing is True


class TestTracingNotInitializedWhenAbsent:
    """Verify tracing is NOT initialized when --enable-tracing is absent."""

    @patch("k8s_scale_test.cli.asyncio")
    @patch("k8s_scale_test.cli._make_aws_session")
    @patch("k8s_scale_test.cli._setup_logging")
    @patch("k8s_scale_test.cli._print_report")
    def test_init_tracing_not_called_when_flag_absent(
        self, mock_report, mock_logging, mock_aws, mock_asyncio
    ):
        mock_aws.return_value = MagicMock()
        mock_summary = MagicMock()
        mock_asyncio.run.return_value = mock_summary

        with patch("k8s_scale_test.cli.ScaleTestController", create=True) as mock_ctrl_cls, \
             patch("k8s_scale_test.cli.EvidenceStore", create=True), \
             patch("kubernetes.config.load_kube_config"), \
             patch("kubernetes.client"), \
             patch("k8s_scale_test.tracing.init_tracing") as mock_init, \
             patch("k8s_scale_test.tracing.shutdown") as mock_shutdown:
            mock_ctrl_cls.return_value.run = MagicMock()

            from k8s_scale_test.cli import main
            main(["run", "--target-pods", "100"])

            # init_tracing must NOT be called when flag is absent
            mock_init.assert_not_called()
            # shutdown must NOT be called either
            mock_shutdown.assert_not_called()


class TestGracefulHandlingWhenInitTracingFails:
    """Verify CLI continues when init_tracing returns False."""

    @patch("k8s_scale_test.cli.asyncio")
    @patch("k8s_scale_test.cli._make_aws_session")
    @patch("k8s_scale_test.cli._setup_logging")
    @patch("k8s_scale_test.cli._print_report")
    def test_continues_when_init_tracing_returns_false(
        self, mock_report, mock_logging, mock_aws, mock_asyncio
    ):
        mock_aws.return_value = MagicMock()
        mock_summary = MagicMock()
        mock_asyncio.run.return_value = mock_summary

        with patch("k8s_scale_test.cli.ScaleTestController", create=True) as mock_ctrl_cls, \
             patch("k8s_scale_test.cli.EvidenceStore", create=True), \
             patch("kubernetes.config.load_kube_config"), \
             patch("kubernetes.client"), \
             patch("k8s_scale_test.tracing.init_tracing", return_value=False) as mock_init, \
             patch("k8s_scale_test.tracing.shutdown") as mock_shutdown, \
             patch("k8s_scale_test.tracing.get_trace_url", return_value="https://example.com/trace"):
            mock_ctrl_cls.return_value.run = MagicMock()

            # Should NOT raise — the CLI logs a warning and continues
            from k8s_scale_test.cli import main
            main(["run", "--target-pods", "100", "--enable-tracing"])

            mock_init.assert_called_once()
            # Controller.run() should still be called
            mock_asyncio.run.assert_called_once()
            # shutdown is still called because enable_tracing flag is set
            mock_shutdown.assert_called_once()
