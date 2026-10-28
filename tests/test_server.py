import json
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import threading
import unittest

from himrakshak.risk import MODEL_ID
from himrakshak.server import HimRakshakServer


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.server = HimRakshakServer(("127.0.0.1", 0), seed=42)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None, raw_body=None):
        connection = HTTPConnection(self.host, self.port, timeout=3)
        encoded = raw_body if raw_body is not None else json.dumps(body).encode("utf-8")
        request_headers = {"Content-Type": "application/json", "Connection": "close"}
        if headers:
            request_headers.update(headers)
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        if response.getheader("Content-Type", "").startswith("application/json"):
            return response.status, json.loads(raw) if raw else None
        return response.status, raw

    def packet(self, **overrides):
        packet = {
            "node_id": "HR-01",
            "sequence": 0,
            "timestamp": "2026-10-27T12:30:00Z",
            "soil_moisture_pct": 32.4,
            "tilt_deg": 0.9,
            "vibration_level": 11.1,
            "battery_pct": 82.0,
            "transport": "PYTHON_SIMULATOR",
        }
        packet.update(overrides)
        return packet

    def test_health_and_initial_state(self):
        status, health = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["model_id"], MODEL_ID)
        self.assertTrue(health["illustrative"])
        status, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIsNone(state["latest"])
        self.assertEqual(state["events"], [])

    def test_canonical_and_alias_ingestion(self):
        status, result = self.request("POST", "/api/readings", self.packet())
        self.assertEqual(status, 200)
        self.assertEqual(set(result), {"reading", "assessment"})
        self.assertEqual(set(result["assessment"]), {"risk_level", "demo_score", "reasons", "model_id", "illustrative"})
        alias = self.packet()
        alias.update(
            {"node": alias.pop("node_id"), "moisture": alias.pop("soil_moisture_pct"),
             "tilt": alias.pop("tilt_deg"), "vibration": alias.pop("vibration_level")}
        )
        status, result = self.request("POST", "/api/readings", alias)
        self.assertEqual(status, 200)
        self.assertEqual(set(result["reading"]) & {"node", "moisture", "tilt", "vibration"}, set())

    def test_forged_metadata_and_rejection_preserve_state(self):
        self.request("POST", "/api/readings", self.packet(soil_moisture_pct=78.4, tilt_deg=4.2))
        status, state_before = self.request("GET", "/api/state")
        rejected = self.packet(soil_moisture_pct=True, risk="NORMAL", demo_score=0, reasons=[])
        status, error = self.request("POST", "/api/readings", rejected)
        self.assertEqual(status, 400)
        self.assertIn("error", error)
        status, state_after = self.request("GET", "/api/state")
        self.assertEqual(state_after, state_before)

    def test_malformed_non_object_conflict_and_size_errors(self):
        status, _ = self.request(
            "POST", "/api/readings", headers={"Content-Length": "7"}, raw_body=b"invalid"
        )
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/api/readings", ["not", "object"])
        self.assertEqual(status, 400)
        conflict = self.packet(moisture=99)
        status, _ = self.request("POST", "/api/readings", conflict)
        self.assertEqual(status, 400)
        status, _ = self.request(
            "POST",
            "/api/readings",
            headers={"Content-Length": str(65 * 1024)},
            raw_body=b"x" * (65 * 1024),
        )
        self.assertEqual(status, 413)

    def test_simulation_scenarios_and_sequences(self):
        expected = {"normal": "NORMAL", "heavy_rain": "CRITICAL", "slope_shift": "CRITICAL", "vibration_burst": "WARNING"}
        sequences = []
        for scenario, level in expected.items():
            status, result = self.request("POST", "/api/simulate", {"scenario": scenario})
            self.assertEqual(status, 200)
            self.assertEqual(result["assessment"]["risk_level"], level)
            sequences.append(result["reading"]["sequence"])
        self.assertEqual(sequences, [0, 1, 2, 3])

    def test_concurrent_simulation_requests_keep_unique_sequences(self):
        def simulate(_):
            return self.request("POST", "/api/simulate", {"scenario": "normal"})

        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(simulate, range(8)))
        self.assertTrue(all(status == 200 for status, _ in responses))
        sequences = sorted(result["reading"]["sequence"] for _, result in responses)
        self.assertEqual(sequences, list(range(8)))

    def test_routes_and_methods(self):
        self.assertEqual(self.request("GET", "/api/unknown")[0], 404)
        self.assertEqual(self.request("GET", "/missing")[0], 404)
        self.assertEqual(self.request("PUT", "/api/health")[0], 405)
        self.assertEqual(self.request("GET", "/")[0], 200)

    def test_station_roster_detail_and_isolation(self):
        status, overview = self.request("GET", "/api/stations")
        self.assertEqual(status, 200)
        self.assertEqual([item["node_id"] for item in overview["stations"]], ["HR-01", "HR-02", "HR-03"])
        status, first = self.request("POST", "/api/stations/HR-02/simulate", {"scenario": "heavy_rain"})
        self.assertEqual(status, 200)
        self.assertEqual(first["reading"]["node_id"], "HR-02")
        self.assertEqual(first["reading"]["sequence"], 0)
        status, detail = self.request("GET", "/api/stations/HR-02")
        self.assertEqual(status, 200)
        self.assertEqual(detail["station"]["event_count"], 1)
        status, untouched = self.request("GET", "/api/stations/HR-01")
        self.assertEqual(status, 200)
        self.assertIsNone(untouched["station"]["latest"])

    def test_station_reading_identity_and_legacy_route(self):
        status, result = self.request("POST", "/api/stations/HR-03/readings", self.packet(node_id="HR-03"))
        self.assertEqual(status, 200)
        self.assertEqual(result["reading"]["sequence"], 0)
        status, error = self.request("POST", "/api/stations/HR-03/readings", self.packet(node_id="HR-01"))
        self.assertEqual(status, 400)
        self.assertIn("node_id", error["error"]["message"])
        status, legacy = self.request("POST", "/api/simulate", {"scenario": "normal", "station": "HR-01"})
        self.assertEqual(status, 200)
        self.assertEqual(legacy["reading"]["node_id"], "HR-01")

    def test_unknown_station_and_global_history_bound(self):
        self.assertEqual(self.request("GET", "/api/stations/HR-99")[0], 404)
        self.assertEqual(self.request("POST", "/api/stations/HR-99/simulate", {"scenario": "normal"})[0], 404)
        for _ in range(105):
            status, _ = self.request("POST", "/api/stations/HR-01/simulate", {"scenario": "normal"})
            self.assertEqual(status, 200)
        status, overview = self.request("GET", "/api/stations")
        self.assertEqual(status, 200)
        self.assertEqual(len(overview["events"]), 100)

    def test_reliability_fault_routes_and_state_preservation(self):
        packet = self.packet(sequence=0)
        status, dropped = self.request(
            "POST", "/api/stations/HR-01/faults/drop", packet
        )
        self.assertEqual(status, 200)
        self.assertEqual(dropped["outcome"], "dropped")
        status, detail = self.request("GET", "/api/stations/HR-01")
        self.assertEqual(detail["station"]["event_count"], 0)

        status, pending = self.request(
            "POST", "/api/stations/HR-01/faults/delay", packet
        )
        self.assertEqual(status, 200)
        self.assertEqual(pending["outcome"], "pending")
        status, detail = self.request("GET", "/api/stations/HR-01")
        self.assertEqual(detail["station"]["communication"]["pending_packets"], 1)
        status, released = self.request(
            "POST", "/api/stations/HR-01/faults/release", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(released["outcome"], "delivered")
        self.assertEqual(released["released_order"], 1)

        status, duplicate = self.request(
            "POST",
            "/api/stations/HR-01/faults/duplicate",
            self.packet(sequence=0),
        )
        self.assertEqual(status, 200)
        self.assertEqual(duplicate["outcome"], "duplicate")
        status, second = self.request(
            "POST",
            "/api/stations/HR-01/faults/deliver",
            self.packet(sequence=1, soil_moisture_pct=70),
        )
        self.assertEqual(status, 200)
        self.assertEqual(second["outcome"], "delivered")
        status, stale = self.request(
            "POST", "/api/stations/HR-01/faults/stale", self.packet(sequence=0)
        )
        self.assertEqual(status, 200)
        self.assertEqual(stale["outcome"], "stale")

    def test_reliability_disconnect_reconnect_and_station_isolation(self):
        status, disconnected = self.request(
            "POST", "/api/stations/HR-02/faults/disconnect", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(disconnected["outcome"], "disconnected")
        status, rejected = self.request(
            "POST", "/api/stations/HR-02/simulate", {"scenario": "normal"}
        )
        self.assertEqual(status, 409)
        status, untouched = self.request("GET", "/api/stations/HR-01")
        self.assertIsNone(untouched["station"]["latest"])
        status, reconnected = self.request(
            "POST", "/api/stations/HR-02/faults/reconnect", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(reconnected["outcome"], "reconnected")
        status, accepted = self.request(
            "POST", "/api/stations/HR-02/simulate", {"scenario": "normal"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(accepted["reading"]["sequence"], 0)

    def test_reliability_invalid_requests_are_structured(self):
        status, error = self.request(
            "POST", "/api/stations/HR-01/faults/delay", {"reading": {"node_id": "HR-01"}}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", error)
        status, error = self.request(
            "POST", "/api/stations/HR-99/faults/drop", self.packet()
        )
        self.assertEqual(status, 404)
        status, error = self.request(
            "POST", "/api/stations/HR-01/faults/unknown", {}
        )
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
