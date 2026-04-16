"""
broker_ctrader.py — IC Markets / cTrader Open API connector.

Uses the official `ctrader-open-api` Python package (Twisted-based TCP).
The Twisted reactor runs in a background daemon thread so it does not
block the Telegram bot's main thread.

Credentials required per user:
  client_id      : OAuth2 client ID from cTrader Open API portal
  client_secret  : OAuth2 client secret
  access_token   : OAuth2 access token (obtained from broker portal)
  account_id     : cTrader account ID (numeric, e.g. 12345678)
  environment    : "demo" or "live"

IC Markets uses standard lot sizes:
  1 lot = 100,000 units (major forex)
  volume is passed in lots (e.g. 0.01 for micro lot)
"""

import threading
import time

from broker_base import BrokerBase

# Host constants
CTRADER_HOSTS = {
    "demo": ("demo.ctraderapi.com", 5035),
    "live": ("live.ctraderapi.com", 5036),
}

# Module-level Twisted reactor management
_reactor_lock    = threading.Lock()
_reactor_started = False
_reactor_thread  = None


def _ensure_reactor():
    global _reactor_started, _reactor_thread
    with _reactor_lock:
        if not _reactor_started:
            from twisted.internet import reactor

            def _run():
                reactor.run(installSignalHandlers=False)

            _reactor_thread  = threading.Thread(target=_run, daemon=True)
            _reactor_thread.start()
            _reactor_started = True
            time.sleep(0.5)   # give reactor a moment to start


class CTraderConnector(BrokerBase):
    """
    Synchronous wrapper around the async cTrader Open API.
    All public methods block until a response is received (or timeout).
    """

    TIMEOUT = 15   # seconds to wait for each API response

    def __init__(self):
        self._client       = None
        self._account_id   = None
        self._connected    = False
        self._lock         = threading.Lock()

    @property
    def broker_name(self) -> str:
        return "IC Markets (cTrader)"

    def normalize_symbol(self, symbol: str) -> str:
        import re
        return re.sub(r"[^A-Z0-9]", "", symbol.upper())

    # ── Connection ─────────────────────────────────────────────────────────────

    def connect(self, credentials: dict) -> dict:
        """
        credentials: access_token, account_id, environment ("demo" | "live")
        client_id and client_secret fall back to env vars CTRADER_CLIENT_ID /
        CTRADER_CLIENT_SECRET if not supplied in the credentials dict.
        """
        import os, json as _json
        # Priority: credentials dict → env vars → bot_data.json persisted config
        _bot_data_cfg: dict = {}
        try:
            _bd_path = os.path.join(os.path.dirname(__file__), "bot_data.json")
            if os.path.exists(_bd_path):
                _raw = _json.loads(open(_bd_path).read())
                _bot_data_cfg = _raw.get("ctrader_app_config", {})
        except Exception:
            pass

        client_id    = (credentials.get("client_id",    "") or
                        os.getenv("CTRADER_CLIENT_ID",  "") or
                        _bot_data_cfg.get("client_id",  "")).strip()
        client_sec   = (credentials.get("client_secret","") or
                        os.getenv("CTRADER_CLIENT_SECRET","") or
                        _bot_data_cfg.get("client_secret","")).strip()
        access_token = credentials.get("access_token", "").strip()
        account_id   = str(credentials.get("account_id", "")).strip()
        env          = credentials.get("environment", "demo").strip().lower()

        if not client_id or not client_sec:
            return {"ok": False,
                    "error": "cTrader app credentials not configured. "
                             "Admin: run /setctraderapp <client_id> <client_secret> "
                             "or set CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET env vars."}
        if not access_token or not account_id:
            return {"ok": False, "error": "Missing access_token or account_id"}
        if env not in CTRADER_HOSTS:
            env = "demo"

        host, port = CTRADER_HOSTS[env]
        self._account_id = int(account_id)

        try:
            _ensure_reactor()

            from twisted.internet import reactor
            from ctrader_open_api import Client, Protobuf, TcpProtocol
            import ctrader_open_api.messages.OpenApiMessages_pb2 as pb

            connect_event = threading.Event()
            auth_event    = threading.Event()
            error_holder  = [None]

            self._client = Client(host, port, TcpProtocol)

            # ── Response handler ──────────────────────────────────────────────
            def _on_message(client, message):
                msg_type = message.payloadType
                if msg_type == pb.ProtoOAApplicationAuthRes().payloadType:
                    connect_event.set()
                elif msg_type == pb.ProtoOAAccountAuthRes().payloadType:
                    auth_event.set()
                elif msg_type == pb.ProtoOAErrorRes().payloadType:
                    error = Protobuf.extract(message, pb.ProtoOAErrorRes)
                    error_holder[0] = error.description
                    connect_event.set()
                    auth_event.set()

            def _on_error(failure):
                error_holder[0] = str(failure)
                connect_event.set()
                auth_event.set()

            self._client.setConnectedCallback(
                lambda client, __: reactor.callLater(0.1, _send_app_auth)
            )
            self._client.setMessageReceivedCallback(_on_message)
            self._client.setDisconnectedCallback(lambda *a: None)

            def _send_app_auth():
                req = pb.ProtoOAApplicationAuthReq()
                req.clientId     = client_id
                req.clientSecret = client_sec
                self._client.send(req)

            def _do_connect():
                self._client.startService()

            reactor.callFromThread(_do_connect)

            if not connect_event.wait(self.TIMEOUT):
                return {"ok": False, "error": "cTrader: app auth timed out"}
            if error_holder[0]:
                return {"ok": False, "error": f"cTrader error: {error_holder[0]}"}

            # ── Account auth ──────────────────────────────────────────────────
            def _send_account_auth():
                req = pb.ProtoOAAccountAuthReq()
                req.ctidTraderAccountId = self._account_id
                req.accessToken         = access_token
                self._client.send(req)

            reactor.callFromThread(_send_account_auth)

            if not auth_event.wait(self.TIMEOUT):
                return {"ok": False, "error": "cTrader: account auth timed out"}
            if error_holder[0]:
                return {"ok": False, "error": f"cTrader error: {error_holder[0]}"}

            self._connected = True
            print(f"[cTrader] Connected — account {account_id} ({env})", flush=True)
            return {"ok": True, "error": ""}

        except Exception as e:
            return {"ok": False, "error": str(e)}

    def disconnect(self):
        try:
            if self._client:
                from twisted.internet import reactor
                reactor.callFromThread(self._client.stopService)
        except Exception:
            pass
        self._connected = False
        print("[cTrader] Disconnected.", flush=True)

    def is_connected(self) -> bool:
        return self._connected

    # ── Internal send helper ──────────────────────────────────────────────────

    def _send_and_wait(self, request, response_type_instance):
        """
        Send a protobuf request and block until the matching response arrives.
        Returns (response_obj | None, error_str).
        """
        from twisted.internet import reactor
        import ctrader_open_api.messages.OpenApiMessages_pb2 as pb
        from ctrader_open_api import Protobuf

        event     = threading.Event()
        result    = [None, None]   # [response, error]
        resp_type = response_type_instance.payloadType

        prev_callback = self._client.getMessageReceivedCallback()

        def _handler(client, message):
            if message.payloadType == resp_type:
                result[0] = Protobuf.extract(message, type(response_type_instance))
                event.set()
            elif message.payloadType == pb.ProtoOAErrorRes().payloadType:
                err = Protobuf.extract(message, pb.ProtoOAErrorRes)
                result[1] = err.description
                event.set()
            elif prev_callback:
                prev_callback(client, message)

        self._client.setMessageReceivedCallback(_handler)

        def _send():
            self._client.send(request)

        reactor.callFromThread(_send)
        event.wait(self.TIMEOUT)
        self._client.setMessageReceivedCallback(prev_callback)

        return result[0], result[1]

    # ── Account ────────────────────────────────────────────────────────────────

    def get_account_info(self) -> dict:
        import ctrader_open_api.messages.OpenApiMessages_pb2 as pb
        req = pb.ProtoOATraderReq()
        req.ctidTraderAccountId = self._account_id
        try:
            resp, err = self._send_and_wait(req, pb.ProtoOATraderRes())
            if err or not resp:
                return {"balance": 0, "equity": 0, "currency": "USD",
                        "open_trade_count": 0, "error": err or "No response"}
            trader = resp.trader
            return {
                "balance":          trader.balance / 100.0,  # cTrader stores in cents
                "equity":           trader.balance / 100.0,  # approximate
                "margin_used":      0,
                "currency":         "USD",
                "open_trade_count": 0,
                "error":            "",
            }
        except Exception as e:
            return {"balance": 0, "equity": 0, "currency": "USD",
                    "open_trade_count": 0, "error": str(e)}

    # ── Market state ──────────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> dict:
        import ctrader_open_api.messages.OpenApiMessages_pb2 as pb
        sym = self.normalize_symbol(symbol)
        req = pb.ProtoOAGetTickDataReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId = 0  # resolved by name in practice
        try:
            resp, err = self._send_and_wait(req, pb.ProtoOAGetTickDataRes())
            if err or not resp:
                return {"bid": 0, "ask": 0, "spread": 0, "error": err or "No price"}
            ticks = list(resp.tickData)
            if ticks:
                bid = ticks[-1].bid / 100000.0
                ask = ticks[-1].ask / 100000.0
                return {"bid": bid, "ask": ask,
                        "spread": round(ask - bid, 6), "error": ""}
            return {"bid": 0, "ask": 0, "spread": 0, "error": "No tick data"}
        except Exception as e:
            return {"bid": 0, "ask": 0, "spread": 0, "error": str(e)}

    # ── Order management ──────────────────────────────────────────────────────

    def place_market_order(self, symbol: str, direction: str,
                            units: float, sl: float, tp: float) -> dict:
        import ctrader_open_api.messages.OpenApiMessages_pb2 as pb
        sym = self.normalize_symbol(symbol)
        # cTrader volume is in lots * 100 (e.g. 0.01 lot = 1)
        volume = int(round(units * 100))
        try:
            req = pb.ProtoOANewOrderReq()
            req.ctidTraderAccountId = self._account_id
            req.symbolName  = sym
            req.orderType   = pb.MARKET
            req.tradeSide   = pb.BUY if direction == "BUY" else pb.SELL
            req.volume      = volume
            if sl > 0:
                req.stopLoss   = int(sl * 100000)
            if tp > 0:
                req.takeProfit = int(tp * 100000)

            resp, err = self._send_and_wait(req, pb.ProtoOAExecutionEvent())
            if err:
                return {"ok": False, "order_id": "", "fill_price": 0, "error": err}
            if resp and resp.HasField("position"):
                pos = resp.position
                return {
                    "ok":         True,
                    "order_id":   str(pos.positionId),
                    "fill_price": pos.price / 100000.0,
                    "error":      "",
                }
            return {"ok": False, "order_id": "", "fill_price": 0,
                    "error": "Order not confirmed"}
        except Exception as e:
            return {"ok": False, "order_id": "", "fill_price": 0, "error": str(e)}

    def modify_trade(self, order_id: str, sl: float, tp: float) -> dict:
        import ctrader_open_api.messages.OpenApiMessages_pb2 as pb
        try:
            req = pb.ProtoOAAmendOrderReq()
            req.ctidTraderAccountId = self._account_id
            req.positionId = int(order_id)
            if sl > 0:
                req.stopLoss   = int(sl * 100000)
            if tp > 0:
                req.takeProfit = int(tp * 100000)
            resp, err = self._send_and_wait(req, pb.ProtoOAExecutionEvent())
            if err:
                return {"ok": False, "error": err}
            return {"ok": True, "error": ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def close_trade(self, order_id: str, units: float = None) -> dict:
        import ctrader_open_api.messages.OpenApiMessages_pb2 as pb
        try:
            req = pb.ProtoOAClosePositionReq()
            req.ctidTraderAccountId = self._account_id
            req.positionId = int(order_id)
            req.volume = int((units or 0) * 100) or 0
            resp, err = self._send_and_wait(req, pb.ProtoOAExecutionEvent())
            if err:
                return {"ok": False, "close_price": 0, "error": err}
            close_price = 0
            if resp and resp.HasField("position"):
                close_price = resp.position.price / 100000.0
            return {"ok": True, "close_price": close_price, "error": ""}
        except Exception as e:
            return {"ok": False, "close_price": 0, "error": str(e)}

    def get_open_trades(self) -> list:
        import ctrader_open_api.messages.OpenApiMessages_pb2 as pb
        try:
            req = pb.ProtoOAReconcileReq()
            req.ctidTraderAccountId = self._account_id
            resp, err = self._send_and_wait(req, pb.ProtoOAReconcileRes())
            if err or not resp:
                return []
            trades = []
            for pos in resp.position:
                direction = "BUY" if pos.tradeSide == pb.BUY else "SELL"
                trades.append({
                    "order_id":       str(pos.positionId),
                    "symbol":         pos.symbolName,
                    "direction":      direction,
                    "units":          pos.volume / 100.0,
                    "open_price":     pos.price / 100000.0,
                    "sl":             (pos.stopLoss or 0) / 100000.0,
                    "tp":             (pos.takeProfit or 0) / 100000.0,
                    "unrealized_pnl": (pos.swap or 0) / 100.0,
                })
            return trades
        except Exception as e:
            print(f"[cTrader] get_open_trades error: {e}", flush=True)
            return []
