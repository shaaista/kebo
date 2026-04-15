(function () {
  "use strict";

  function findLoaderScript() {
    if (document.currentScript) {
      return document.currentScript;
    }
    var scripts = document.getElementsByTagName("script");
    for (var i = scripts.length - 1; i >= 0; i -= 1) {
      var src = String(scripts[i].src || "");
      if (/kebo-widget-loader\.js(?:\?|$)/i.test(src)) {
        return scripts[i];
      }
    }
    return null;
  }

  function parseNumber(value, fallback, min, max) {
    var num = Number(value);
    if (!Number.isFinite(num)) {
      return fallback;
    }
    return Math.min(max, Math.max(min, Math.round(num)));
  }

  function parseBoolean(value, fallback) {
    var raw = String(value == null ? "" : value).trim().toLowerCase();
    if (!raw) {
      return fallback;
    }
    if (raw === "1" || raw === "true" || raw === "yes" || raw === "on") {
      return true;
    }
    if (raw === "0" || raw === "false" || raw === "no" || raw === "off") {
      return false;
    }
    return fallback;
  }

  function normalizePosition(value) {
    return String(value || "").trim().toLowerCase() === "left" ? "left" : "right";
  }

  function safeOrigin(value, fallback) {
    try {
      return new URL(String(value || ""), fallback).origin;
    } catch (_err) {
      return fallback;
    }
  }

  function normalizeColor(value, fallback) {
    var raw = String(value || "").trim();
    if (!raw) {
      return fallback;
    }
    if (/^[0-9a-fA-F]{3,8}$/.test(raw)) {
      return "#" + raw;
    }
    if (/^#[0-9a-fA-F]{3,8}$/.test(raw)) {
      return raw;
    }
    return raw;
  }

  var script = findLoaderScript();
  if (!script) {
    return;
  }

  var scriptUrl = safeOrigin(script.src || window.location.href, window.location.origin);
  var data = script.dataset || {};
  var hostOrigin = safeOrigin(data.host || scriptUrl, window.location.origin);

  var config = {
    widgetId: String(data.widgetId || "default").trim() || "default",
    hotelCode: String(data.hotelCode || data.propertyCode || "default").trim().toLowerCase() || "default",
    phase: String(data.phase || "pre_booking").trim().toLowerCase() || "pre_booking",
    position: normalizePosition(data.position),
    width: parseNumber(data.width, 380, 280, 600),
    height: parseNumber(data.height, 620, 360, 900),
    offset: parseNumber(data.offset, 20, 8, 64),
    zIndex: parseNumber(data.zIndex, 2147482000, 1000, 2147483647),
    brandColor: normalizeColor(data.brandColor, "#C72C41"),
    accentColor: normalizeColor(data.accentColor, "#2563eb"),
    bgColor: normalizeColor(data.bgColor, "#f8fafc"),
    textColor: normalizeColor(data.textColor, "#1e293b"),
    botName: String(data.botName || "Assistant").trim() || "Assistant",
    autoOpen: parseBoolean(data.autoOpen, false),
  };

  var frameUrl = new URL("/chat", hostOrigin);
  frameUrl.searchParams.set("embed", "1");
  frameUrl.searchParams.set("widget_id", config.widgetId);
  frameUrl.searchParams.set("hotel_code", config.hotelCode);
  frameUrl.searchParams.set("phase", config.phase);
  frameUrl.searchParams.set("brand_color", config.brandColor);
  frameUrl.searchParams.set("accent_color", config.accentColor);
  frameUrl.searchParams.set("bg_color", config.bgColor);
  frameUrl.searchParams.set("text_color", config.textColor);
  frameUrl.searchParams.set("bot_name", config.botName);
  frameUrl.searchParams.set("position", config.position);
  frameUrl.searchParams.set("width", String(config.width));
  frameUrl.searchParams.set("height", String(config.height));

  if (data.sessionId) {
    frameUrl.searchParams.set("session_id", String(data.sessionId).trim());
  }
  if (data.apiBase) {
    frameUrl.searchParams.set("api_base", String(data.apiBase).trim());
  }

  var root = document.createElement("div");
  root.setAttribute("data-kebo-widget-root", "true");
  root.style.position = "fixed";
  root.style.zIndex = String(config.zIndex);
  root.style.pointerEvents = "none";
  root.style.width = config.width + "px";
  root.style.height = config.height + "px";
  root.style.overflow = "visible";

  var frameContainer = document.createElement("div");
  frameContainer.setAttribute("data-kebo-widget-frame", "true");
  frameContainer.style.position = "absolute";
  frameContainer.style.bottom = "0";
  frameContainer.style.width = config.width + "px";
  frameContainer.style.height = config.height + "px";
  frameContainer.style.maxWidth = "min(calc(100vw - 16px), 600px)";
  frameContainer.style.maxHeight = "min(calc(100vh - 16px), 900px)";
  frameContainer.style.background = config.bgColor;
  frameContainer.style.border = "1px solid rgba(15, 23, 42, 0.12)";
  frameContainer.style.borderRadius = "16px";
  frameContainer.style.boxShadow = "0 24px 60px rgba(15, 23, 42, 0.30)";
  frameContainer.style.overflow = "hidden";
  frameContainer.style.pointerEvents = "none";
  frameContainer.style.opacity = "0";
  frameContainer.style.transform = "translateY(14px) scale(0.985)";
  frameContainer.style.transformOrigin = config.position === "left" ? "bottom left" : "bottom right";
  frameContainer.style.transition = "opacity 180ms ease, transform 180ms ease";

  if (config.position === "left") {
    frameContainer.style.left = "0";
  } else {
    frameContainer.style.right = "0";
  }

  var iframe = document.createElement("iframe");
  iframe.src = frameUrl.toString();
  iframe.title = config.botName + " Chat Widget";
  iframe.setAttribute("loading", "lazy");
  iframe.setAttribute("allow", "clipboard-read; clipboard-write");
  iframe.style.display = "block";
  iframe.style.width = "100%";
  iframe.style.height = "100%";
  iframe.style.border = "0";
  iframe.style.background = config.bgColor;

  frameContainer.appendChild(iframe);

  var launcher = document.createElement("button");
  launcher.type = "button";
  launcher.setAttribute("aria-label", "Open chat");
  launcher.setAttribute("data-kebo-widget-launcher", "true");
  launcher.style.display = "flex";
  launcher.style.alignItems = "center";
  launcher.style.justifyContent = "center";
  launcher.style.width = "58px";
  launcher.style.height = "58px";
  launcher.style.position = "absolute";
  launcher.style.bottom = "0";
  launcher.style.border = "0";
  launcher.style.borderRadius = "999px";
  launcher.style.cursor = "pointer";
  launcher.style.pointerEvents = "auto";
  launcher.style.background = config.brandColor;
  launcher.style.boxShadow = "0 16px 34px rgba(15, 23, 42, 0.34)";
  launcher.style.transition = "transform 140ms ease, opacity 140ms ease";
  launcher.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M12 3C7.03 3 3 6.58 3 11c0 2.1.9 4.02 2.38 5.45L4.4 21l4.8-1.58c.9.22 1.84.33 2.8.33 4.97 0 9-3.58 9-8s-4.03-8-9-8Zm0 14.5c-.84 0-1.67-.11-2.45-.34l-.58-.17-2.25.74.48-2.08-.4-.42A6.05 6.05 0 0 1 5 11c0-3.1 3.13-5.75 7-5.75S19 7.9 19 11s-3.13 6.5-7 6.5Z" fill="white"/></svg>';

  if (config.position === "left") {
    launcher.style.left = "0";
    launcher.style.right = "auto";
  } else {
    launcher.style.right = "0";
    launcher.style.left = "auto";
  }

  root.appendChild(frameContainer);
  root.appendChild(launcher);

  var isOpen = Boolean(config.autoOpen);

  function applyState() {
    if (isOpen) {
      frameContainer.style.opacity = "1";
      frameContainer.style.pointerEvents = "auto";
      frameContainer.style.transform = "translateY(0) scale(1)";
      launcher.style.opacity = "0";
      launcher.style.pointerEvents = "none";
      launcher.style.transform = "scale(0.9)";
    } else {
      frameContainer.style.opacity = "0";
      frameContainer.style.pointerEvents = "none";
      frameContainer.style.transform = "translateY(14px) scale(0.985)";
      launcher.style.opacity = "1";
      launcher.style.pointerEvents = "auto";
      launcher.style.transform = "scale(1)";
    }
  }

  function openWidget() {
    isOpen = true;
    applyState();
  }

  function closeWidget() {
    isOpen = false;
    applyState();
  }

  function toggleWidget() {
    isOpen = !isOpen;
    applyState();
  }

  function destroyWidget() {
    window.removeEventListener("message", onMessage);
    window.removeEventListener("keydown", onKeyDown);
    window.removeEventListener("resize", applyAnchorAndSize);
    if (window.visualViewport) {
      window.visualViewport.removeEventListener("resize", applyAnchorAndSize);
      window.visualViewport.removeEventListener("scroll", applyAnchorAndSize);
    }
    if (root.parentNode) {
      root.parentNode.removeChild(root);
    }
    if (window.KeboWidget === api) {
      delete window.KeboWidget;
    }
  }

  function onMessage(event) {
    if (event.origin !== hostOrigin) {
      return;
    }
    var payload = event.data;
    if (!payload || typeof payload !== "object") {
      return;
    }
    if (payload.source !== "kebo-widget") {
      return;
    }
    if (payload.type === "widget:close" || payload.type === "close") {
      closeWidget();
      return;
    }
    if (payload.type === "widget:open" || payload.type === "open") {
      openWidget();
      return;
    }
    if (payload.type === "widget:toggle" || payload.type === "toggle") {
      toggleWidget();
    }
  }

  function onKeyDown(event) {
    if (event.key === "Escape" && isOpen) {
      closeWidget();
    }
  }

  launcher.addEventListener("click", openWidget);
  window.addEventListener("message", onMessage);
  window.addEventListener("keydown", onKeyDown);

  var api = {
    version: "1.0.0",
    config: config,
    open: openWidget,
    close: closeWidget,
    toggle: toggleWidget,
    destroy: destroyWidget,
    isOpen: function () {
      return isOpen;
    },
  };

  function getViewportWidth() {
    return Math.max(320, Math.floor(window.innerWidth || document.documentElement.clientWidth || 1024));
  }

  function getViewportHeight() {
    return Math.max(360, Math.floor(window.innerHeight || document.documentElement.clientHeight || 768));
  }

  function getBottomOffset() {
    var base = config.offset;
    var vv = window.visualViewport;
    if (!vv) {
      return base;
    }
    var keyboardDelta = Math.max(0, (window.innerHeight || vv.height) - vv.height - vv.offsetTop);
    if (!Number.isFinite(keyboardDelta) || keyboardDelta <= 0) {
      return base;
    }
    return Math.max(base, base + Math.round(keyboardDelta));
  }

  function applyAnchorAndSize() {
    var vw = getViewportWidth();
    var vh = getViewportHeight();
    var width = Math.min(config.width, Math.max(260, vw - 16));
    var height = Math.min(config.height, Math.max(320, vh - 16));

    root.style.width = width + "px";
    root.style.height = height + "px";
    frameContainer.style.width = width + "px";
    frameContainer.style.height = height + "px";

    root.style.bottom = getBottomOffset() + "px";
    if (config.position === "left") {
      root.style.left = config.offset + "px";
      root.style.right = "auto";
    } else {
      root.style.right = config.offset + "px";
      root.style.left = "auto";
    }
  }

  function mountRoot() {
    if (!document.body) {
      return;
    }
    document.body.appendChild(root);
    applyAnchorAndSize();
    applyState();
    if (isOpen) {
      openWidget();
    }
  }

  if (document.body) {
    mountRoot();
  } else {
    window.addEventListener("DOMContentLoaded", mountRoot, { once: true });
  }

  window.addEventListener("resize", applyAnchorAndSize, { passive: true });
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", applyAnchorAndSize, { passive: true });
    window.visualViewport.addEventListener("scroll", applyAnchorAndSize, { passive: true });
  }

  window.KeboWidget = api;
})();
