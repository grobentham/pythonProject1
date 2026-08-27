from __future__ import annotations

import math
import re

from v53_policy import as_float, downside_stop_distance, getv, instruction_kind, instruction_text


def apply_v56_semantic_hardening(engine_cls):
    original_scope = engine_cls._scope_tickets
    original_risk_accepts = engine_cls._risk_accepts

    def patched_scope(self, ins, states=("OPEN", "PENDING")):
        # The compiler should emit scope_entry_price for named-entry actions. As
        # a defensive fallback, infer it only for CLOSE actions. Do NOT use this
        # inference for a MOVE_SL action from the same multi-action message,
        # because the SL instruction should apply to the survivor(s), not the
        # ticket being closed.
        kind = instruction_kind(ins)
        explicit_entry = getv(ins, "entry_price", "scope_entry_price", default=None)
        if explicit_entry is None and "CLOSE" in kind:
            text = instruction_text(ins)
            match = re.search(
                r"(?i)\bclose\s+(?:the\s+)?(?:entry|order)\s*[@:$]?\s*([1-9]\d{3}(?:\.\d{1,3})?)\b",
                text,
            )
            if match:
                if isinstance(ins, dict):
                    ins = dict(ins)
                    ins["scope_entry_price"] = float(match.group(1))
                else:
                    # Non-dict compiler rows are expected to carry the structured
                    # field. If they do not, fail closed rather than mutating an
                    # opaque object.
                    self.rejections["NAMED_ENTRY_SCOPE_MISSING_STRUCTURED_FIELD"] += 1
                    return []
        return original_scope(self, ins, states=states)

    def patched_risk_accepts(self, side, entry, sl, ms, bid, ask):
        enabled = getv(self.policy, "risk_cap_enabled", default=None)
        if enabled is False:
            equity, _, _ = self._equity_margin(bid, ask, ms)
            candidate = downside_stop_distance(side, entry, sl) * self.spec.ounces * self._rate(ms)
            return True, candidate, math.inf

        configured = as_float(getv(self.policy, "max_reserved_stop_risk_pct", default=None))
        if configured is None:
            return original_risk_accepts(self, side, entry, sl, ms, bid, ask)

        equity, _, _ = self._equity_margin(bid, ask, ms)
        candidate = downside_stop_distance(side, entry, sl) * self.spec.ounces * self._rate(ms)
        cap = max(0.0, equity * configured / 100.0)
        return self._reserved_risk_sgd(ms) + candidate <= cap + 1e-9, candidate, cap

    engine_cls._scope_tickets = patched_scope
    engine_cls._risk_accepts = patched_risk_accepts
    return engine_cls
