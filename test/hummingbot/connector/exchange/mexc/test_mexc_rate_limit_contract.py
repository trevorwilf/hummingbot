"""
MEXC Rate-Limit Contract Test — validates endpoint weights match expected values.
This is a RELEASE GATE test.
"""
import unittest
from hummingbot.connector.exchange.mexc import mexc_constants as CONSTANTS

class TestMexcRateLimitContract(unittest.TestCase):
    # Expected weights based on MEXC Spot V3 docs (verified 2026-03-26)
    # MEXC does not expose weights via API headers, so these are from docs.
    EXPECTED_WEIGHTS = {
        CONSTANTS.EXCHANGE_INFO_PATH_URL: 25,
        CONSTANTS.SNAPSHOT_PATH_URL: 3,
        CONSTANTS.SERVER_TIME_PATH_URL: 1,
        CONSTANTS.PING_PATH_URL: 1,
    }

    def _get_ip_weight(self, limit_id):
        for rl in CONSTANTS.RATE_LIMITS:
            if rl.limit_id == limit_id:
                for pair in rl.linked_limits:
                    if pair.limit_id == CONSTANTS.IP_REQUEST_WEIGHT:
                        return pair.weight
        return None

    def test_public_endpoint_weights_match_expected(self):
        mismatches = []
        for limit_id, expected in self.EXPECTED_WEIGHTS.items():
            actual = self._get_ip_weight(limit_id)
            if actual is None:
                mismatches.append(f"  {limit_id}: NOT FOUND in RATE_LIMITS")
            elif actual != expected:
                mismatches.append(f"  {limit_id}: expected={expected}, actual={actual}")
        if mismatches:
            self.fail("MEXC rate-limit weights STALE:\n" + "\n".join(mismatches))

    def test_order_rate_limit_exists(self):
        order_limits = [rl for rl in CONSTANTS.RATE_LIMITS
                        if rl.limit_id == CONSTANTS.ORDER_RATE_LIMIT_ID]
        self.assertEqual(1, len(order_limits))
        self.assertGreater(order_limits[0].limit, 0)

    def test_weight_manifest_printed(self):
        print("\n=== MEXC Rate-Limit Weight Manifest ===")
        for rl in CONSTANTS.RATE_LIMITS:
            weights = {p.limit_id: p.weight for p in rl.linked_limits}
            if weights:
                print(f"  {rl.limit_id}: {weights}")
        print("=== End ===\n")
