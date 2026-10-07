"""Per-asset Model 2 scanner.

A Model 2 scanner *is* an :class:`~scanner.AssetScanner` — same warm-up, same
dedupe, same persistence, same AI overlay, same Telegram alert, same safe
executor, same event log, same setup-state publication. The only difference is
which engine it drives, so this is a subclass that supplies that engine and
changes nothing else.

Everything downstream is therefore shared rather than reimplemented: with
``AUTO_TRADING=false`` — the default, and the only value the tests use — a Model
2 signal writes a database row, is analysed, is alerted and records a ``SKIPPED``
execution attempt, exactly as a Model 1 signal does. Nothing in the pipeline
branches on the model except the alert's own presentation, and that branch lives
in the notifier, not here.

The engine is injectable for the same reason the parent's is: a test can drive a
Model 2 scanner with a stub engine, and the live loop can pass the real one.
"""
from __future__ import annotations

from config import Settings, get_settings
from trading.asset_manager import Asset
from trading.model2 import MODEL_2, Model2Strategy

from scanner import AssetScanner


class Model2Scanner(AssetScanner):
    """Run one asset through the Model 2 engine and the shared pipeline."""

    #: The model this scanner speaks for. Read by the live loop and the
    #: dashboard so a published setup table can say which engine produced it.
    MODEL = MODEL_2

    def __init__(self, asset: Asset, *, settings: Settings | None = None,
                 engine: Model2Strategy | None = None, **kwargs):
        cfg = settings or get_settings()
        # ``log=self._strategy_log`` is only needed when this class builds the
        # engine itself: it routes the engine's decision lines to the console and
        # the event log, exactly as the parent does for Model 1. A caller who
        # injects an engine owns how that engine was built, so nothing is
        # overridden here.
        super().__init__(
            asset,
            settings=cfg,
            engine=engine or Model2Strategy(asset, settings=cfg,
                                            log=self._strategy_log),
            **kwargs,
        )

    @property
    def model(self) -> str:
        """``"MODEL_2"`` — this scanner's engine, not the parent's."""
        return self.MODEL

    def liquidity_log(self) -> list[str]:
        """The session levels currently driving this asset, for diagnostics.

        Read-only, and never consulted by a decision — it exists so an operator
        can see *what* the engine is watching without reading the database.
        Returns ``"<label> <price>"`` lines, newest session first.
        """
        try:
            return [f"{lvl.label} {lvl.price}"
                    for lvl in self.engine.session_levels()]
        except Exception:  # a half-built engine must not break a poll
            return []
