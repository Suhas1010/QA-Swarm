const state = {
  products: [],
  cart: [],
  user: { email: "demo@novacart.test", name: "Demo Shopper", bio: "", logged_in: false },
  orders: [],
  tickets: [],
  discount: 0,
  coupon: null,
  checkoutShipping: null,
  searchQuery: "",
  sortMode: "default",
};

function navigate(path) {
  window.history.pushState({}, "", path);
  renderRoute();
}

async function loadBootstrap() {
  const res = await fetch("/api/bootstrap");
  const data = await res.json();
  state.products = data.products || [];
  state.cart = data.state.cart || [];
  state.user = data.state.user || state.user;
  state.orders = data.state.orders || [];
  state.tickets = data.state.tickets || [];
  state.discount = data.state.discount || 0;
  state.coupon = data.state.coupon || null;
  updateNavState();
}

function updateNavState() {
  const countEl = document.getElementById("nav-cart-count");
  if (countEl) {
    const count = state.cart.reduce((acc, item) => acc + Number(item.qty || 0), 0);
    countEl.textContent = String(count);
  }
  const loginLink = document.getElementById("nav-login-link");
  if (loginLink) {
    loginLink.textContent = state.user.logged_in ? state.user.name : "Login";
  }
}

function attachRouteLinks(root = document) {
  root.querySelectorAll("a[data-route]").forEach((anchor) => {
    anchor.onclick = (e) => {
      e.preventDefault();
      navigate(anchor.getAttribute("data-route"));
    };
  });
}

async function renderHome(main) {
  // TRAP-A: 14% transient console error on home load
  if (Math.random() < 0.14) {
    console.error("Analytics beacon flush timeout (transient)");
  }

  let recsHtml = "<p class='muted'>Loading recommendations...</p>";
  main.innerHTML = `
    <section class="hero">
      <h1>Workspace Essentials, Upgraded</h1>
      <p>Precision keyboards, crisp displays, and modular docks built for engineers.</p>
      <div class="row" style="margin-top: 1rem;">
        <button id="hero-shop-btn" type="button">Browse Catalog</button>
        <button id="hero-cart-btn" type="button" class="btn-secondary">View Cart</button>
      </div>
    </section>
    <section class="card" style="margin-bottom: 1.5rem;">
      <h2>Recommended For You</h2>
      <div id="recs-slot">${recsHtml}</div>
    </section>
  `;
  document.getElementById("hero-shop-btn").onclick = () => navigate("/catalog");
  document.getElementById("hero-cart-btn").onclick = () => navigate("/cart");

  try {
    const res = await fetch("/api/recommendations");
    if (res.ok) {
      const data = await res.json();
      const slot = document.getElementById("recs-slot");
      if (slot) {
        slot.innerHTML = `<div class="card-grid">${data.items
          .map(
            (p) => `
          <div class="card">
            <img class="product-img" src="${p.image}" alt="${p.name}" />
            <h3 class="product-title">${p.name}</h3>
            <p class="muted">$${p.price.toFixed(2)}</p>
          </div>`
          )
          .join("")}</div>`;
      }
    }
  } catch {
    // ignore transient recommendation load failure
  }
}

function renderCatalog(main) {
  let items = [...state.products];
  if (state.searchQuery.trim()) {
    const q = state.searchQuery.trim().toLowerCase();
    items = items.filter((p) => p.name.toLowerCase().includes(q));
  }
  if (state.sortMode === "price_asc") {
    items.sort((a, b) => a.price - b.price);
  }

  // TRAP-C: harmless 120ms shimmer pulse
  main.innerHTML = `
    <div class="space-between" style="margin-bottom: 1rem;">
      <h1>Product Catalog</h1>
      <div class="row">
        <input id="catalog-search" placeholder="Search gear..." value="${state.searchQuery.replace(/"/g, "&quot;")}" style="width: 220px;" />
        <button id="search-btn" type="button">Search</button>
        <select id="sort-select" style="width: 180px;">
          <option value="default" ${state.sortMode === "default" ? "selected" : ""}>Sort: Featured</option>
          <option value="price_asc" ${state.sortMode === "price_asc" ? "selected" : ""}>Price: Low to High</option>
          <option value="rating">Sort: Highest Rated</option>
        </select>
      </div>
    </div>
    <div id="search-status" class="muted" style="margin-bottom: 1rem;"></div>
    <div class="card-grid shimmer">
      ${items
        .map(
          (p) => `
        <article class="card">
          <img class="product-img" src="${p.image}" alt="${p.name}" />
          <h3 class="product-title">${p.name}</h3>
          <p class="muted">$${p.price.toFixed(2)} · ★ ${p.rating}</p>
          <div class="row" style="margin-top: 0.75rem;">
            <button type="button" class="add-cart-btn" data-id="${p.id}">Add to Cart</button>
            <button type="button" class="quick-view-btn btn-secondary" data-id="${p.id}">Quick View</button>
          </div>
        </article>`
        )
        .join("")}
    </div>
  `;

  // BUG-17: Unescaped innerHTML enables reflected XSS on search
  if (state.searchQuery.trim()) {
    document.getElementById("search-status").innerHTML =
      "Showing results for: " + state.searchQuery;
  }

  document.getElementById("search-btn").onclick = () => {
    state.searchQuery = document.getElementById("catalog-search").value;
    renderCatalog(main);
  };

  document.getElementById("sort-select").onchange = (e) => {
    const val = e.target.value;
    if (val === "rating") {
      // BUG-03: ReferenceError calling undefined sortByRating()
      sortByRating();
    }
    state.sortMode = val;
    renderCatalog(main);
  };

  main.querySelectorAll(".add-cart-btn").forEach((btn) => {
    btn.onclick = async () => {
      const productId = Number(btn.getAttribute("data-id"));
      const res = await fetch("/api/cart/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ product_id: productId, qty: 1 }),
      });
      const data = await res.json();
      state.cart = data.cart;
      updateNavState();
      btn.textContent = "Added!";
      setTimeout(() => {
        btn.textContent = "Add to Cart";
      }, 900);
    };
  });

  main.querySelectorAll(".quick-view-btn").forEach((btn) => {
    btn.onclick = () => {
      const productId = Number(btn.getAttribute("data-id"));
      const product = state.products.find((p) => p.id === productId);
      // BUG-01: Product 101 has meta: null -> TypeError reading 'specs'
      const specsText = product.meta.specs;
      document.getElementById("qv-title").textContent = product.name;
      document.getElementById("qv-body").innerHTML = `
        <p><strong>Price:</strong> $${product.price.toFixed(2)}</p>
        <p><strong>Specifications:</strong> ${specsText}</p>
      `;
      document.getElementById("quick-view-modal").classList.remove("hidden");
    };
  });
}

function renderCart(main) {
  const subtotal = state.cart.reduce((sum, i) => sum + i.price * i.qty, 0);
  const total = subtotal - state.discount;
  if (total < 0) {
    // BUG-08 observable console signal alongside negative total UI
    console.error("Negative total detected in cart state:", total);
  }

  main.innerHTML = `
    <h1>Shopping Cart</h1>
    <div class="card" style="margin-bottom: 1.25rem;">
      ${
        state.cart.length === 0
          ? `<p class="muted">Your cart is empty. <a href="/catalog" data-route="/catalog">Browse products</a>.</p>`
          : state.cart
              .map(
                (item) => `
          <div class="space-between" style="padding: 0.75rem 0; border-bottom: 1px solid var(--border);">
            <div>
              <strong>${item.name}</strong>
              <div class="muted">$${item.price.toFixed(2)} each</div>
            </div>
            <div class="row">
              <label for="qty-${item.product_id}" style="margin: 0;">Qty</label>
              <input id="qty-${item.product_id}" class="qty-input" data-id="${item.product_id}" type="number" value="${item.qty}" style="width: 85px;" />
            </div>
          </div>`
              )
              .join("")
      }
      <div class="row" style="margin-top: 1rem;">
        <input id="coupon-input" placeholder="Promo code (Try SAVE50 or WELCOME10)" style="width: 280px;" />
        <button id="apply-coupon-btn" type="button" class="btn-secondary">Apply Promo</button>
      </div>
      <div id="coupon-msg" class="muted" style="margin-top: 0.5rem;"></div>
      <div class="alert" style="margin-top: 1.25rem;">
        <strong>Free Standard Shipping</strong> on workspace orders over $50. Estimated delivery: 2-4 business days.
        <div class="muted" style="margin-top: 0.35rem;">Includes 30-day hardware return guarantee and serialized warranty coverage.</div>
      </div>
      <div class="space-between" style="margin-top: 2.5rem; padding-top: 1.25rem; border-top: 1px solid var(--border);">
        <div>
          <div>Subtotal: $${subtotal.toFixed(2)}</div>
          <div>Discount: -$${state.discount.toFixed(2)}</div>
          <strong id="cart-total-display">Total: $${total.toFixed(2)}</strong>
        </div>
        <button id="proceed-checkout-btn" type="button">Proceed to Checkout</button>
      </div>
    </div>
  `;
  attachRouteLinks(main);

  main.querySelectorAll(".qty-input").forEach((input) => {
    input.onchange = () => {
      const productId = Number(input.getAttribute("data-id"));
      const raw = input.value.trim();
      const qty = Number(raw);
      // BUG-02: Typing 0 or empty throws uncaught RangeError
      if (!raw || qty === 0 || Number.isNaN(qty)) {
        throw new RangeError("Cart line quantity must be positive");
      }
      // BUG-08: Negative qty is accepted without client validation
      fetch("/api/cart/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ product_id: productId, qty }),
      })
        .then((res) => res.json())
        .then((data) => {
          state.cart = data.cart;
          updateNavState();
          renderCart(main);
        });
    };
  });

  document.getElementById("apply-coupon-btn").onclick = async () => {
    const code = document.getElementById("coupon-input").value;
    // BUG-04: SAVE50 returns HTTP 500
    const res = await fetch("/api/coupon", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    if (res.ok) {
      const data = await res.json();
      state.discount = data.discount;
      renderCart(main);
    } else {
      document.getElementById("coupon-msg").textContent = `Coupon failed (${res.status})`;
    }
  };

  document.getElementById("proceed-checkout-btn").onclick = () => {
    navigate("/checkout");
  };
}

function renderCheckout(main) {
  const params = new URLSearchParams(window.location.search);
  const step = params.get("step") || "1";

  if (step === "2") {
    // BUG-14: Direct /checkout?step=2 or back-nav finds checkoutShipping null -> TypeError
    const buyerName = state.checkoutShipping.full_name.toUpperCase();
    const total = state.cart.reduce((sum, i) => sum + i.price * i.qty, 0) - state.discount;

    main.innerHTML = `
      <h1>Checkout — Step 2: Review &amp; Place Order</h1>
      <div class="card">
        <p><strong>Recipient:</strong> ${buyerName}</p>
        <p><strong>Address:</strong> ${state.checkoutShipping.address}, ${state.checkoutShipping.zip_code}</p>
        <p><strong>Items:</strong> ${state.cart.length} | <strong>Total:</strong> $${total.toFixed(2)}</p>
        <div class="row" style="margin-top: 1rem;">
          <button id="checkout-back-btn" type="button" class="btn-secondary">Back to Shipping</button>
          <button id="place-order-btn" type="button">Place Order</button>
        </div>
      </div>
    `;

    document.getElementById("checkout-back-btn").onclick = () => {
      state.checkoutShipping = null;
      navigate("/checkout");
    };

    // BUG-13: Does not disable button; double-click fires duplicate POST /api/orders -> 500 & blanks <main>
    let orderSubmitCount = 0;
    document.getElementById("place-order-btn").onclick = async () => {
      orderSubmitCount += 1;
      const attempt = orderSubmitCount;
      const res = await fetch("/api/orders", { method: "POST" });
      if (!res.ok) {
        main.innerHTML = "";
        return;
      }
      const data = await res.json();
      if (attempt === 1 && orderSubmitCount > 1) {
        main.innerHTML = "";
        return;
      }
      state.orders.unshift(data.order);
      state.cart = [];
      state.discount = 0;
      state.checkoutShipping = null;
      updateNavState();
      main.innerHTML = `
        <div class="card alert-success">
          <h2>Order Confirmed: ${data.order.id}</h2>
          <p>Total charged: $${data.order.total.toFixed(2)}</p>
          <button id="view-orders-btn" type="button">View Orders</button>
        </div>
      `;
      document.getElementById("view-orders-btn").onclick = () => navigate("/orders");
    };
    return;
  }

  main.innerHTML = `
    <h1>Checkout — Step 1: Shipping &amp; Payment</h1>
    <div class="card" style="max-width: 560px;">
      <div class="form-group">
        <label for="chk-name">Full Name</label>
        <input id="chk-name" placeholder="Alex Rivera" />
      </div>
      <div class="form-group">
        <label for="chk-address">Street Address</label>
        <input id="chk-address" placeholder="742 Evergreen Terrace" />
      </div>
      <div class="row">
        <div class="form-group" style="flex: 1;">
          <label for="chk-zip">ZIP Code</label>
          <input id="chk-zip" placeholder="94107" />
        </div>
        <div class="form-group" style="flex: 1;">
          <label for="chk-exp">Card Expiry (MM/YY)</label>
          <input id="chk-exp" placeholder="12/28" />
        </div>
      </div>
      <button id="checkout-next-btn" type="button">Continue to Review</button>
    </div>
  `;

  document.getElementById("checkout-next-btn").onclick = async () => {
    const payload = {
      full_name: document.getElementById("chk-name").value,
      address: document.getElementById("chk-address").value,
      zip_code: document.getElementById("chk-zip").value,
      card_expiry: document.getElementById("chk-exp").value,
    };
    // BUG-09: No client validation; server returns 400 and UI fails silently
    const res = await fetch("/api/checkout/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      state.checkoutShipping = payload;
      navigate("/checkout?step=2");
    }
  };
}

function renderOrders(main) {
  main.innerHTML = `
    <h1>Order History</h1>
    <div class="card-grid" style="grid-template-columns: 1fr;">
      ${state.orders
        .map(
          (o) => `
        <div class="card space-between">
          <div>
            <strong>${o.id}</strong> · <span class="muted">${o.date} · ${o.status}</span>
            <div>Items: ${o.items.join(", ")}</div>
            <div>Total: $${Number(o.total).toFixed(2)}</div>
          </div>
          <div class="row">
            <button type="button" class="track-pkg-btn btn-secondary">Track Package</button>
            <button type="button" class="download-inv-btn" data-id="${o.invoiceId}">Download Invoice</button>
          </div>
        </div>`
        )
        .join("")}
    </div>
    <div id="orders-feedback" class="muted" style="margin-top: 1rem;"></div>
  `;

  // BUG-16: Dead button — click produces zero DOM, URL, or network change
  main.querySelectorAll(".track-pkg-btn").forEach((btn) => {
    btn.onclick = () => {
      // intentional no-op dead control
    };
  });

  // BUG-06: data-id is "undefined" -> GET /api/orders/invoice/undefined (404)
  main.querySelectorAll(".download-inv-btn").forEach((btn) => {
    btn.onclick = async () => {
      const invId = btn.getAttribute("data-id");
      const res = await fetch(`/api/orders/invoice/${invId}`);
      if (!res.ok) {
        document.getElementById("orders-feedback").textContent = `Invoice error: HTTP ${res.status}`;
      }
    };
  });
}

function renderProfile(main) {
  main.innerHTML = `
    <h1>Account Profile &amp; Preferences</h1>
    <div class="card" style="max-width: 600px; margin-bottom: 1.5rem;">
      <div class="form-group">
        <label for="prof-name">Display Name</label>
        <input id="prof-name" value="${state.user.name}" />
      </div>
      <div class="form-group">
        <label for="prof-bio">Bio</label>
        <textarea id="prof-bio" rows="3">${state.user.bio}</textarea>
      </div>
      <button id="save-profile-btn" type="button">Save Profile</button>
      <div id="profile-status" class="muted" style="margin-top: 0.5rem;"></div>
    </div>

    <div class="card" style="max-width: 600px;">
      <h2>Newsletter Signup</h2>
      <div class="row">
        <input id="newsletter-email" placeholder="you@company.com" style="flex: 1;" />
        <button id="newsletter-btn" type="button">Subscribe</button>
      </div>
      <div id="newsletter-status" class="muted" style="margin-top: 0.5rem;"></div>
    </div>
  `;

  document.getElementById("save-profile-btn").onclick = async () => {
    const name = document.getElementById("prof-name").value;
    const bio = document.getElementById("prof-bio").value;
    // BUG-05: bio > 200 chars triggers HTTP 500
    const res = await fetch("/api/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, bio }),
    });
    if (res.ok) {
      const data = await res.json();
      state.user = data.user;
      updateNavState();
      document.getElementById("profile-status").textContent = "Profile saved.";
    } else {
      document.getElementById("profile-status").textContent = `Save failed (${res.status})`;
    }
  };

  document.getElementById("newsletter-btn").onclick = async () => {
    const email = document.getElementById("newsletter-email").value;
    // BUG-07: Accepts whitespace / invalid email client-side and sends to server -> 422 + console error
    const res = await fetch("/api/newsletter", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    if (res.ok) {
      document.getElementById("newsletter-status").textContent = "Subscribed!";
    } else {
      console.error("Newsletter subscription failed: invalid email");
      document.getElementById("newsletter-status").textContent = `Subscription failed (${res.status})`;
    }
  };
}

function renderSupport(main) {
  main.innerHTML = `
    <h1>Customer Support</h1>
    <div class="card" style="max-width: 620px; margin-bottom: 1.5rem;">
      <div class="form-group">
        <label for="tck-subject">Ticket Subject</label>
        <input id="tck-subject" placeholder="Describe your issue briefly" />
      </div>
      <div class="form-group">
        <label for="tck-msg">Message</label>
        <textarea id="tck-msg" rows="3" placeholder="Include order number if applicable..."></textarea>
      </div>
      <button id="submit-ticket-btn" type="button">Submit Ticket</button>
    </div>

    <h2>Recent Tickets</h2>
    <div id="ticket-list" class="card-grid" style="grid-template-columns: 1fr;">
      ${state.tickets
        .map(
          (t) => `
        <div class="card">
          <strong>${t.id}</strong> — <span>${t.subject}</span> <span class="muted">(${t.status})</span>
        </div>`
        )
        .join("")}
    </div>
  `;

  document.getElementById("submit-ticket-btn").onclick = async () => {
    const subject = document.getElementById("tck-subject").value;
    const message = document.getElementById("tck-msg").value;
    const res = await fetch("/api/tickets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subject, message }),
    });
    const data = await res.json();
    state.tickets.unshift(data.ticket);
    // BUG-18: Renders raw unescaped HTML in ticket subject -> XSS execution
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `<strong>${data.ticket.id}</strong> — ${data.ticket.subject} <span class="muted">(${data.ticket.status})</span>`;
    document.getElementById("ticket-list").prepend(card);
  };
}

function renderLogin(main) {
  main.innerHTML = `
    <h1>Sign In to NovaCart</h1>
    <div class="card" style="max-width: 440px;">
      <p class="muted">Demo account: <code>demo@novacart.test</code> / <code>Password123!</code></p>
      <div class="form-group">
        <label for="login-email">Email</label>
        <input id="login-email" type="email" placeholder="demo@novacart.test" />
      </div>
      <div class="form-group">
        <label for="login-password">Password</label>
        <input id="login-password" type="password" placeholder="Password123!" />
      </div>
      <button id="login-submit-btn" type="button">Sign In</button>
      <div id="login-status" class="muted" style="margin-top: 0.75rem;"></div>
    </div>
  `;

  document.getElementById("login-submit-btn").onclick = async () => {
    const email = document.getElementById("login-email").value;
    const password = document.getElementById("login-password").value;
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (res.ok) {
      const data = await res.json();
      state.user = data.user;
      updateNavState();
      navigate("/");
    } else {
      document.getElementById("login-status").textContent = "Invalid email or password.";
    }
  };
}

function renderRoute() {
  const main = document.getElementById("app-main");
  const path = window.location.pathname;
  document.querySelectorAll(".nav-links a").forEach((a) => {
    a.classList.toggle("active", a.getAttribute("data-route") === path);
  });

  if (path === "/" || path === "") {
    renderHome(main);
  } else if (path === "/catalog") {
    renderCatalog(main);
  } else if (path === "/cart") {
    renderCart(main);
  } else if (path === "/checkout") {
    renderCheckout(main);
  } else if (path === "/orders") {
    renderOrders(main);
  } else if (path === "/profile") {
    renderProfile(main);
  } else if (path === "/support") {
    renderSupport(main);
  } else if (path === "/login") {
    renderLogin(main);
  } else {
    main.innerHTML = `<div class="card"><h1>404 - Page Not Found</h1><p>Route ${path} does not exist.</p></div>`;
  }
}

window.addEventListener("popstate", renderRoute);

document.getElementById("qv-close").onclick = () => {
  document.getElementById("quick-view-modal").classList.add("hidden");
};

(async function init() {
  attachRouteLinks();
  await loadBootstrap();
  renderRoute();
})();
