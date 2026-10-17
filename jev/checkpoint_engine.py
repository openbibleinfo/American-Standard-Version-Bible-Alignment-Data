"""Isolated candidate ending at first finalization, retaining every warning."""
from general_engine import Engine


class CheckpointEngine(Engine):
    def repair_rejected(self, owners):
        # Returning identical ownership also prevents affected-unit rebuilding.
        # Keep prior validity receipts, pending entries, heads/status and notes.
        return dict(owners)

    def run(self):
        result = super().run()
        result['experimental_endpoint'] = 'first_finalization_with_warnings'
        result['late_repair_executed'] = False
        return result
