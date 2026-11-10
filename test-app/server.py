from __future__ import annotations

import copy
import random
import uuid
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

app = FastAPI(title="NovaCart Seeded Buggy Test App", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

INITIAL_PRODUCTS = [
    {
        "id": 101,
        "name": "Nova Mechanical Keyboard",
        "price": 129.00,
        "rating": 4.8,
        "category": "Peripherals",
        "image": "/static/img_keyboard.svg",
        "meta": None,  # BUG-01: Quick View assumes product.meta.specs exists
    },
    {
        "id": 102,
        "name": "Pulse Wireless Mouse",
        "price": 69.00,
        "rating": 4.6,
        "category": "Peripherals",
        "image": "/static/img_mouse.svg",
        "meta": {"specs": "8000 DPI, USB-C, 58g ultralight"},
    },
    {
        "id": 103,
        "name": "UltraWide-4K-HDR-Creator-Monitor-Without-Spaces-Edition-2026-Pro-Max-Series",
        "price": 499.00,
        "rating": 4.9,
        "category": "Displays",
        "image": "/static/img_monitor.svg",
        "meta": {"specs": "34-inch IPS, 144Hz, Thunderbolt 4"},
    },
    {
        "id": 104,
        "name": "Studio USB-C Dock",
        "price": 149.00,
        "rating": 4.5,
        "category": "Accessories",
        "image": "/static/img_dock.svg",
        "meta": {"specs": "12-in-1 Dual 4K DisplayPort, 100W PD"},
    },
]

DEFAULT_ORDERS = [
    {
        "id": "ORD-9041",
        "date": "2026-10-28",
        "status": "Shipped",
        "total": 198.00,
        "items": ["Nova Mechanical Keyboard", "Pulse Wireless Mouse"],
    }
]


def _default_session_state() -> dict[str, Any]:
    return {
        "user": {
            "email": "demo@novacart.test",
            "name": "Demo Shopper",
            "bio": "Exploratory QA tester exploring NovaCart.",
            "logged_in": False,
        },
        "cart": [{"product_id": 102, "name": "Pulse Wireless Mouse", "price": 69.00, "qty": 1}],
        "discount": 0.0,
        "coupon": None,
        "orders": copy.deepcopy(DEFAULT_ORDERS),
        "tickets": [
            {"id": "TCK-101", "subject": "Shipping inquiry for ORD-9041", "status": "Open"}
        ],
        "newsletter": [],
    }


SESSIONS: dict[str, dict[str, Any]] = {}


def _get_session_id(qa_session: str | None) -> str:
    return qa_session or "default"


def _get_state(session_id: str) -> dict[str, Any]:
    if session_id not in SESSIONS:
        SESSIONS[session_id] = _default_session_state()
    return SESSIONS[session_id]


def _ensure_cookie(response: Response, qa_session: str | None) -> str:
    sid = qa_session or f"sess_{uuid.uuid4().hex[:12]}"
    if qa_session is None:
        response.set_cookie("qa_session", sid, httponly=False, samesite="lax")
    return sid


class LoginPayload(BaseModel):
    email: str
    password: str


class CartAddPayload(BaseModel):
    product_id: int
    qty: int = 1


class CartUpdatePayload(BaseModel):
    product_id: int
    qty: int


class CouponPayload(BaseModel):
    code: str


class CheckoutValidatePayload(BaseModel):
    full_name: str = ""
    address: str = ""
    zip_code: str = ""
    card_expiry: str = ""


class ProfilePayload(BaseModel):
    name: str = ""
    bio: str = ""


class NewsletterPayload(BaseModel):
    email: str = ""


class TicketPayload(BaseModel):
    subject: str = ""
    message: str = ""


@app.post("/__qa_reset")
async def qa_reset(qa_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Reset the caller's session or all sessions if requested without cookie."""
    if qa_session and qa_session in SESSIONS:
        SESSIONS[qa_session] = _default_session_state()
        return {"reset": "session", "session_id": qa_session}
    SESSIONS.clear()
    return {"reset": "all"}


@app.get("/api/bootstrap")
async def api_bootstrap(
    response: Response, qa_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    sid = _ensure_cookie(response, qa_session)
    state = _get_state(sid)
    return {
        "session_id": sid,
        "products": INITIAL_PRODUCTS,
        "state": state,
    }


@app.get("/api/recommendations")
async def api_recommendations() -> dict[str, Any]:
    # TRAP-B: 12% transient 503 noise (usually fails 2-of-3 replay)
    if random.random() < 0.12:
        raise HTTPException(status_code=503, detail="Recommendation service transient warm-up")
    return {"items": INITIAL_PRODUCTS[:2]}


@app.post("/api/login")
async def api_login(
    payload: LoginPayload,
    response: Response,
    qa_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    sid = _ensure_cookie(response, qa_session)
    state = _get_state(sid)
    if payload.email == "demo@novacart.test" and payload.password == "Password123!":
        state["user"]["logged_in"] = True
        return {"ok": True, "user": state["user"]}
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.post("/api/cart/add")
async def api_cart_add(
    payload: CartAddPayload,
    response: Response,
    qa_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    sid = _ensure_cookie(response, qa_session)
    state = _get_state(sid)
    product = next((p for p in INITIAL_PRODUCTS if p["id"] == payload.product_id), None)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    for item in state["cart"]:
        if item["product_id"] == payload.product_id:
            item["qty"] += payload.qty
            return {"cart": state["cart"]}
    state["cart"].append(
        {
            "product_id": product["id"],
            "name": product["name"],
            "price": product["price"],
            "qty": payload.qty,
        }
    )
    return {"cart": state["cart"]}


@app.post("/api/cart/update")
async def api_cart_update(
    payload: CartUpdatePayload,
    response: Response,
    qa_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    sid = _ensure_cookie(response, qa_session)
    state = _get_state(sid)
    # BUG-08: Accepts negative qty without validation!
    for item in state["cart"]:
        if item["product_id"] == payload.product_id:
            item["qty"] = payload.qty
    return {"cart": state["cart"]}


@app.post("/api/coupon")
async def api_coupon(
    payload: CouponPayload,
    response: Response,
    qa_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    sid = _ensure_cookie(response, qa_session)
    state = _get_state(sid)
    code = payload.code.strip().upper()
    if code == "SAVE50":
        # BUG-04: Promo code SAVE50 crashes with HTTP 500
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal promo engine crash applying coupon SAVE50"},
        )
    if code == "WELCOME10":
        state["coupon"] = code
        state["discount"] = 10.0
        return {"ok": True, "discount": 10.0}
    raise HTTPException(status_code=400, detail="Unknown coupon code")


@app.post("/api/checkout/validate")
async def api_checkout_validate(payload: CheckoutValidatePayload) -> dict[str, Any]:
    # BUG-09: Client has no validation; invalid ZIP or past expiry fails with 400
    if not payload.zip_code.isdigit() or len(payload.zip_code) < 5:
        raise HTTPException(status_code=400, detail="ZIP code must be at least 5 digits")
    if payload.card_expiry.strip() in {"01/19", "01/20", ""}:
        raise HTTPException(status_code=400, detail="Card expiry is missing or expired")
    if not payload.full_name.strip() or not payload.address.strip():
        raise HTTPException(status_code=400, detail="Shipping name and address are required")
    return {"valid": True}


@app.post("/api/orders")
async def api_orders_create(
    response: Response, qa_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    sid = _ensure_cookie(response, qa_session)
    state = _get_state(sid)
    # BUG-13: Double-clicking Place Order sends second request after cart clears -> 409/500
    if not state["cart"]:
        return JSONResponse(
            status_code=500,
            content={"detail": "Cannot place order with empty cart (duplicate submission)"},
        )
    total = sum(i["price"] * i["qty"] for i in state["cart"]) - state["discount"]
    order = {
        "id": f"ORD-{random.randint(1000, 9999)}",
        "date": "2026-11-10",
        "status": "Confirmed",
        "total": round(total, 2),
        "items": [i["name"] for i in state["cart"]],
    }
    state["orders"].insert(0, order)
    state["cart"] = []
    state["discount"] = 0.0
    state["coupon"] = None
    return {"order": order}


@app.get("/api/orders/invoice/{order_id}")
async def api_order_invoice(order_id: str) -> Response:
    # BUG-06: Frontend passes undefined order_id, hitting 404
    raise HTTPException(status_code=404, detail=f"Invoice for order '{order_id}' not found")


@app.post("/api/profile")
async def api_profile_update(
    payload: ProfilePayload,
    response: Response,
    qa_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    sid = _ensure_cookie(response, qa_session)
    state = _get_state(sid)
    # BUG-05: Bio > 200 chars triggers unhandled 500 database truncation error
    if len(payload.bio) > 200:
        return JSONResponse(
            status_code=500,
            content={"detail": "Unhandled DB error: bio column overflow (>200 chars)"},
        )
    state["user"]["name"] = payload.name or state["user"]["name"]
    state["user"]["bio"] = payload.bio
    return {"ok": True, "user": state["user"]}


@app.post("/api/newsletter")
async def api_newsletter(
    payload: NewsletterPayload,
    response: Response,
    qa_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    sid = _ensure_cookie(response, qa_session)
    state = _get_state(sid)
    # BUG-07: Accepts whitespace-only email cleanly in UI, fails validation on server (422)
    if not payload.email.strip() or "@" not in payload.email:
        raise HTTPException(status_code=422, detail="Newsletter email cannot be blank or invalid")
    state["newsletter"].append(payload.email.strip())
    return {"ok": True, "email": payload.email.strip()}


@app.post("/api/tickets")
async def api_tickets(
    payload: TicketPayload,
    response: Response,
    qa_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    sid = _ensure_cookie(response, qa_session)
    state = _get_state(sid)
    ticket = {
        "id": f"TCK-{random.randint(200, 999)}",
        "subject": payload.subject,
        "message": payload.message,
        "status": "Open",
    }
    state["tickets"].insert(0, ticket)
    return {"ticket": ticket}


@app.get("/legal/terms-v2")
async def broken_terms_page() -> HTMLResponse:
    # BUG-15: Dead footer link returns 404
    return HTMLResponse(
        content="<h1>404 - Page Not Found</h1><p>Terms &amp; Privacy v2 has moved or does not exist.</p>",
        status_code=404,
    )


@app.get("/{full_path:path}")
async def serve_spa(full_path: str, request: Request) -> Response:
    if full_path.startswith("api/"):
        return JSONResponse(status_code=404, content={"detail": "API route not found"})
    return FileResponse(STATIC_DIR / "index.html")

