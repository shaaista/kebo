# Embeddable Chat Widget

This document covers how to install the Kebo chat widget on any website. The
same script works on plain HTML, React, Next.js, Vue, Svelte, Angular,
WordPress, Shopify, Webflow, Wix, Ghost, and any other platform that allows
custom `<script>` tags.

---

## 1. Get a widget key

In the admin panel:

1. Open **Operations → Widget Embed**.
2. Pick the hotel.
3. Click **New Deployment**, set branding/colors/size.
4. Add the customer's website domain to **Allowed origins** (full origin, e.g.
   `https://hotelx.com`). Without this the widget will refuse to load.
5. Save. Copy the snippet from the **Install snippet** card.

The widget key looks like `wk_AbCDeF12_g3HiJ-K4LMnO`. It is a public
identifier — safe to paste into any website's source.

---

## 2. Install per framework

### Plain HTML

```html
<script
  src="https://YOUR-HOST/static/embed/kebo-widget-loader.js"
  data-widget-key="wk_xxxxxxxxxxxx"
  async
></script>
```

Drop it just before `</body>`.

### React (CRA / Vite)

Either paste the HTML snippet into `public/index.html` (or `index.html` for
Vite), **or** mount it from a component when you only want the widget on
specific routes:

```tsx
import { useEffect } from "react";

export function ChatWidget() {
  useEffect(() => {
    const s = document.createElement("script");
    s.src = "https://YOUR-HOST/static/embed/kebo-widget-loader.js";
    s.dataset.widgetKey = "wk_xxxxxxxxxxxx";
    s.async = true;
    document.body.appendChild(s);
    return () => {
      s.remove();
      (window as any).KeboWidget?.destroy?.();
    };
  }, []);
  return null;
}
```

### Next.js (App or Pages router)

```tsx
import Script from "next/script";

<Script
  src="https://YOUR-HOST/static/embed/kebo-widget-loader.js"
  data-widget-key="wk_xxxxxxxxxxxx"
  strategy="afterInteractive"
/>
```

### Vue 3

```vue
<script setup lang="ts">
import { onMounted, onBeforeUnmount } from "vue";

let scriptEl: HTMLScriptElement | null = null;
onMounted(() => {
  scriptEl = document.createElement("script");
  scriptEl.src = "https://YOUR-HOST/static/embed/kebo-widget-loader.js";
  scriptEl.dataset.widgetKey = "wk_xxxxxxxxxxxx";
  scriptEl.async = true;
  document.body.appendChild(scriptEl);
});
onBeforeUnmount(() => {
  scriptEl?.remove();
  (window as any).KeboWidget?.destroy?.();
});
</script>
```

### WordPress

Paste the HTML snippet into **Appearance → Theme File Editor → footer.php**,
just before `</body>`. Or use a plugin like **Insert Headers and Footers**.

### Shopify

In your theme editor, open `theme.liquid` and paste the snippet just before
`</body>`.

---

## 3. How it works

```
┌─────────────────────┐         ┌────────────────────────────┐
│ customer's website  │         │ your kebo backend          │
│                     │         │                            │
│  <script ...>       │ GET     │  /api/widget/bootstrap     │
│   data-widget-key   │────────►│    → returns theme + url   │
│  </script>          │         │                            │
│                     │ iframe  │  /chat?widget_key=...      │
│   ┌──────────────┐  │────────►│    → React chat UI         │
│   │ launcher btn │  │         │                            │
│   │   + iframe   │  │ chat    │  /api/chat/message         │
│   └──────────────┘  │────────►│    → guarded by origin     │
└─────────────────────┘         └────────────────────────────┘
```

1. Browser loads `kebo-widget-loader.js` from your backend.
2. The loader reads `data-widget-key`, calls `/api/widget/bootstrap`.
3. The backend looks up the deployment, validates the origin against
   `allowed_origins`, and returns theme + iframe URL.
4. The loader injects a fixed-position launcher and iframe into the host page.
5. The iframe loads `/chat?widget_key=...` from your backend (CSP
   `frame-ancestors` restricts who can iframe it).
6. All `/api/chat/*` calls inside the iframe carry `widget_key` (cookie or
   header) — middleware re-validates the origin on every request.

---

## 4. Security model

| Concern | Mechanism |
|---|---|
| Random sites embedding your widget | `allowed_origins` allowlist per deployment |
| Hotel A's snippet reused on Hotel B's site | `widget_key` is deployment-scoped |
| Leaked `widget_key` | **Rotate Key** in admin — old key stops working |
| iframe clickjacking | `Content-Security-Policy: frame-ancestors` set per request |
| Cross-origin credentials | Specific `Access-Control-Allow-Origin` (never `*`) + `SameSite=None;Secure` cookies |

The widget key is **public** — there is no secret to leak. Origin enforcement
is what makes it safe; without `allowed_origins`, anyone with the key could
embed.

---

## 5. Programmatic API

After the widget loads, `window.KeboWidget` is available:

```js
KeboWidget.open();      // open the chat
KeboWidget.close();     // close the chat
KeboWidget.toggle();    // toggle
KeboWidget.isOpen();    // boolean
KeboWidget.destroy();   // remove from DOM
KeboWidget.config;      // resolved config used to mount
```

---

## 6. Overriding server config (debugging only)

Server config wins by default. To override per-page (for debugging or
campaign-specific styling), add `data-*` attrs:

```html
<script
  src="https://YOUR-HOST/static/embed/kebo-widget-loader.js"
  data-widget-key="wk_xxxxxxxxxxxx"
  data-brand-color="#0EA5E9"
  data-position="left"
  data-auto-open="true"
  async
></script>
```

Supported overrides: `brand-color`, `accent-color`, `bg-color`, `text-color`,
`bot-name`, `position` (`left`/`right`), `width`, `height`, `offset`,
`z-index`, `auto-open`, `phase`, `session-id`.

---

## 7. Troubleshooting

**Widget doesn't appear**
- Open devtools → Console. If you see `[kebo-widget] bootstrap failed: 403`,
  the host's origin is not in `allowed_origins`. Add it in admin.
- If you see `bootstrap failed: 404`, the `widget_key` is invalid or inactive.

**Widget loads but chat returns "origin not allowed"**
- The iframe's subsequent `/api/chat/*` calls also check origin. Ensure the
  customer's domain is allowlisted (not just the loader's `Referer`).

**Cookies not persisting on Safari / mobile browsers**
- Safari blocks third-party cookies by default. Sessions inside the iframe
  still work because they're scoped to the chat origin (your backend).
- If you need cross-page persistence on the customer's site, use
  `data-session-id` and persist it yourself.

**CSP errors on the host site**
- Some sites block external scripts via `Content-Security-Policy`. Customer
  must add your script origin to their `script-src` and your iframe origin to
  their `frame-src`.

---

## 8. Rolling out

1. Pilot with one hotel — watch metrics in `/admin/api/observability/events`
   for bootstrap success rate and rejected origins.
2. Verify on at least: plain HTML page, a React SPA, mobile Safari, mobile
   Chrome.
3. Confirm color changes in admin propagate to live sites without the customer
   re-deploying their snippet (this is the value of server-driven bootstrap).
4. Roll out to remaining hotels.
