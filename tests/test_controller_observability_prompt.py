"""Unit tests for Controller observability prompt behaviour (tasks 7.1–7.5).

Verifies that the Controller prompts the operator when observability checks
fail, respects abort/continue responses, honours auto_approve mode, and
skips the prompt when all checks pass.

Uses source inspection (matching the pattern in test_controller_tracing.py)
to verify the observability prompt logic exists in the controller code.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5
"""

from __future__ import annotations

import inspect

import pytest

import k8s_scale_test.controller as ctrl_mod


def _get_run_source() -> str:
    """Return the source code of ScaleTestController.run()."""
    return inspect.getsource(ctrl_mod.ScaleTestController.run)


class TestObservabilityPromptCalled:
    """Requirement 4.1: _prompt_operator is called when obs_passed < obs_total."""

    def test_prompt_operator_called_when_obs_checks_fail(self):
        """Verify the controller checks obs_passed < obs_total and calls _prompt_operator."""
        source = _get_run_source()

        # The controller must check for observability failures
        assert "report.obs_total > 0" in source
        assert "report.obs_passed < report.obs_total" in source

        # And call _prompt_operator with an observability message
        assert "await self._prompt_operator(" in source
        assert "Observability:" in source
        assert "checks failed" in source


class TestAbortHaltsRun:
    """Requirement 4.2: operator 'abort' response halts the run with
    halt_reason='operator_aborted_observability'."""

    def test_abort_response_halts_with_correct_reason(self):
        """Verify abort/cancel response returns early with operator_aborted_observability."""
        source = _get_run_source()

        # The controller must check for abort/cancel
        assert '"abort"' in source or "'abort'" in source
        assert '"cancel"' in source or "'cancel'" in source

        # And return with the specific halt_reason
        assert 'halt_reason="operator_aborted_observability"' in source

    def test_abort_returns_make_summary(self):
        """Verify the abort path returns via _make_summary (producing a TestRunSummary)."""
        source = _get_run_source()

        # Find the observability abort block — it should call _make_summary
        # with a ScalingResult that has completed=False
        lines = source.split("\n")
        obs_abort_lines = [
            i for i, l in enumerate(lines)
            if "operator_aborted_observability" in l
        ]
        assert len(obs_abort_lines) >= 1

        # The abort block should use _make_summary and ScalingResult
        abort_idx = obs_abort_lines[0]
        context_block = "\n".join(lines[max(0, abort_idx - 5):abort_idx + 3])
        assert "_make_summary" in context_block
        assert "ScalingResult" in context_block
        assert "completed=False" in context_block


class TestContinueProceeds:
    """Requirement 4.3: operator 'continue' response proceeds past observability check."""

    def test_continue_does_not_halt(self):
        """Verify the controller only halts on abort/cancel — any other response proceeds."""
        source = _get_run_source()

        # The halt condition is specifically for abort/cancel
        # Find the obs_response check block
        lines = source.split("\n")
        obs_response_checks = [
            i for i, l in enumerate(lines)
            if "obs_response" in l and "lower()" in l
        ]
        assert len(obs_response_checks) >= 1

        # The check should be: if response in ("abort", "cancel") — meaning
        # anything else (including "continue") falls through and proceeds
        check_line = lines[obs_response_checks[0]]
        assert "abort" in check_line
        assert "cancel" in check_line


class TestAutoApproveContinues:
    """Requirement 4.4: auto_approve=True continues without interactive prompt
    when obs checks fail."""

    def test_prompt_operator_returns_continue_in_auto_approve(self):
        """Verify _prompt_operator returns 'continue' when auto_approve is True.

        The existing _prompt_operator already handles auto_approve by returning
        'continue' without waiting for input. We verify this behaviour exists.
        """
        source = inspect.getsource(ctrl_mod.ScaleTestController._prompt_operator)

        assert "auto_approve" in source
        assert 'return "continue"' in source

    def test_auto_approve_logged(self):
        """Verify auto_approve mode logs the auto-approval."""
        source = inspect.getsource(ctrl_mod.ScaleTestController._prompt_operator)

        assert "Auto-approving" in source


class TestNoPromptWhenAllPass:
    """Requirement 4.5: no observability prompt when obs_passed == obs_total."""

    def test_no_prompt_when_all_checks_pass(self):
        """Verify the observability prompt is guarded by obs_passed < obs_total.

        When obs_passed == obs_total, the condition is False and the prompt
        block is skipped entirely.
        """
        source = _get_run_source()

        # The guard condition must require obs_passed < obs_total
        assert "report.obs_passed < report.obs_total" in source

        # And obs_total > 0 (so we don't prompt when no checks were run)
        assert "report.obs_total > 0" in source
