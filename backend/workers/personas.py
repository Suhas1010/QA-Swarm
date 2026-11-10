from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PersonaSpec:
    name: str
    goal: str
    viewport_width: int
    viewport_height: int
    has_touch: bool
    system_prompt: str


PERSONAS: dict[str, PersonaSpec] = {
    "impatient_user": PersonaSpec(
        name="impatient_user",
        goal="Complete core flows quickly, double-clicking action buttons and rapidly hitting Back mid-load.",
        viewport_width=1280,
        viewport_height=800,
        has_touch=False,
        system_prompt=(
            "You are an impatient user who hates waiting. Click primary CTAs rapidly, prefer 'dblclick' "
            "on submit/checkout/order buttons, interrupt async transitions with 'back' or fast clicks, "
            "and test whether duplicate clicks or premature navigation break the app."
        ),
    ),
    "hostile_fuzzer": PersonaSpec(
        name="hostile_fuzzer",
        goal="Probe every input field with XSS payloads, empty/whitespace, negative numbers, and long strings.",
        viewport_width=1280,
        viewport_height=800,
        has_touch=False,
        system_prompt=(
            "You are a hostile fuzzer testing input validation and security. Across search boxes, promo inputs, "
            "quantities, profile bios, newsletter signups, and support forms, inject payloads such as: "
            "'<img src=x onerror=\"window.__qa_xss=true\">', 'SAVE50', '-5', '0', '   ', or a 250-character string, "
            "then submit and inspect the result."
        ),
    ),
    "confused_first_timer": PersonaSpec(
        name="confused_first_timer",
        goal="Explore secondary pages, skip wizard steps, test edge controls, footer links, and non-linear paths.",
        viewport_width=1280,
        viewport_height=800,
        has_touch=False,
        system_prompt=(
            "You are a confused first-time visitor exploring without following the happy path. Visit catalog, "
            "quick views, sort dropdowns, orders, invoice/tracking buttons, footer links, and try navigating "
            "directly or backtracking between steps."
        ),
    ),
    "mobile_viewport_user": PersonaSpec(
        name="mobile_viewport_user",
        goal="Navigate the app on a 390x844 phone screen, checking menus, cart, checkout, and layout responsiveness.",
        viewport_width=390,
        viewport_height=844,
        has_touch=True,
        system_prompt=(
            "You are browsing on a 390x844 mobile phone. Navigate through Home, Catalog, Cart, and Checkout, "
            "try tapping bottom buttons, and set suspected_visual_anomaly=true if elements overlap, clip, "
            "or overflow horizontally off-screen."
        ),
    ),
    "edge_explorer": PersonaSpec(
        name="edge_explorer",
        goal="Systematically cover all routes and interactive controls not yet visited by other flows.",
        viewport_width=1280,
        viewport_height=800,
        has_touch=False,
        system_prompt=(
            "You are a thorough exploratory QA engineer. Visit every route in the navbar and footer, "
            "exercise filters, sorting, forms, modals, and secondary buttons systematically."
        ),
    ),
}


def get_persona(name: str) -> PersonaSpec:
    if name not in PERSONAS:
        available = ", ".join(sorted(PERSONAS))
        raise ValueError(f"Unknown persona '{name}'. Available: {available}")
    return PERSONAS[name]

