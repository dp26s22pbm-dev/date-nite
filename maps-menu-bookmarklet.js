(async () => {
  // Date Nite: send a menu to the importer from your own browser.
  // On a restaurant's page in Google Maps (signed in to Google) it opens the listing's Menu tab, reads every dish and
  // price in each section, and opens the "Add New Venue (from a Google Maps link)" issue form with the Maps link and
  // the menu filled in.
  // On a restaurant's page on Uber Eats or Postmates it lists the menu's sections so you can pick which to send
  // (drinks and desserts are ticked to start, because delivery prices for the mains are often marked up), and opens
  // the "Edit a Venue" issue form, which changes only the price categories the text covers.
  // Nothing is sent until you press "Create" on the form.
  // Installed from maps-menu-bookmarklet.html, which builds the bookmark from this file.
  const REPO = "dp26s22pbm-dev/date-nite";
  const SKIP = ["overview", "photo", "breakfast", "lunch", "brunch", "happy", "kid", "catering", "party", "private", "gift"];
  const URL_BUDGET = 7000; // GitHub refuses very long links, so the menu is kept as "Dish  $price" lines
  const PRICE = /^\$\s?\d{1,3}(?:\.\d{2})?$/;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clean = (t) => (t || "").replace(/\s+/g, " ").trim();
  const issueLink = (fields) => "https://github.com/" + REPO + "/issues/new?" + new URLSearchParams(fields).toString();
  // Shorten text a line at a time until link(text) fits in a GitHub link
  const fit = (full, link) => {
    let text = full;
    let lines = full.split("\n");
    while (link(text).length > URL_BUDGET && lines.length > 6) {
      lines = lines.slice(0, Math.floor(lines.length * 0.9));
      text = lines.join("\n");
    }
    return text;
  };

  const onMaps = /(^|\.)google\.[a-z.]+$/.test(location.hostname) && location.pathname.startsWith("/maps");
  const onDelivery = /(^|\.)(ubereats|postmates)\.com$/.test(location.hostname);
  if (onDelivery) return delivery();
  if (!onMaps) {
    alert("Date Nite: open the restaurant's page in Google Maps, Uber Eats or Postmates first, then click this bookmark.");
    return;
  }
  // Opened now, while the click still counts, so the browser does not block it as a pop-up later
  const win = window.open("about:blank", "_blank");

  const tabs = () => [...document.querySelectorAll('[role="tab"]')];
  const label = (el) => clean(el.getAttribute("aria-label") || el.innerText);
  const menuTab = tabs().find((t) => /^menu$/i.test(clean(t.innerText)) || /^menu( |$)/i.test(label(t)));
  const fail = (msg) => { if (win) win.close(); alert("Date Nite: " + msg); };
  if (!menuTab) {
    return fail("this listing shows no Menu tab. Sign in to Google (signed-out Maps hides it), or the listing may have no menu.");
  }
  const topList = menuTab.closest('[role="tablist"]');
  menuTab.click();
  await sleep(1500);

  // Scroll every scrollable box to its end until the page stops growing; Maps draws more dishes as the panel scrolls
  const scrollAll = async () => {
    let last = -1;
    for (let i = 0; i < 12; i++) {
      for (const el of document.querySelectorAll("div")) {
        if (el.scrollHeight > el.clientHeight + 50 && /auto|scroll/.test(getComputedStyle(el).overflowY)) el.scrollTop = el.scrollHeight;
      }
      await sleep(400);
      const size = document.body.innerText.length;
      if (size === last) break;
      last = size;
    }
  };

  // A dish is the largest box that holds exactly one price; its first line is the dish's name
  const dishes = () => {
    const out = [];
    const seen = new Set();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const price = clean(node.nodeValue);
      if (!PRICE.test(price) || !node.parentElement || !node.parentElement.getClientRects().length) continue;
      const count = (el) => (el.innerText.match(/\$\s?\d{1,3}(?:\.\d{2})?/g) || []).length;
      let row = node.parentElement;
      while (row.parentElement && row.parentElement !== document.body && count(row.parentElement) === 1) row = row.parentElement;
      const name = row.innerText.split("\n").map(clean).find((l) => l && !PRICE.test(l));
      if (!name || name === price) continue;
      const line = name.slice(0, 70) + "  " + price;
      if (!seen.has(line)) { seen.add(line); out.push(line); }
    }
    return out;
  };

  // The Menu tab has its own row of sections (Dinner, Seafood, Breakfast...): the tabs outside the top row
  const sections = tabs().filter((t) => t.closest('[role="tablist"]') !== topList && label(t));
  const wanted = sections.filter((t) => !SKIP.some((w) => label(t).toLowerCase().includes(w))).slice(0, 10);
  const parts = [];
  let total = 0;
  if (wanted.length) {
    for (const tab of wanted) {
      tab.click();
      await sleep(900);
      await scrollAll();
      const found = dishes();
      if (found.length) { parts.push("=== " + label(tab) + " ===\n" + found.join("\n")); total += found.length; }
    }
  } else {
    await scrollAll();
    const found = dishes();
    if (found.length) { parts.push(found.join("\n")); total = found.length; }
  }
  if (total < 6) {
    return fail("found only " + total + " priced dishes on the Menu tab (the importer needs at least 6). " +
                "The menu may be photos only. Nothing was sent.");
  }

  const heading = [...document.querySelectorAll("h1")].map((h) => clean(h.innerText)).find(Boolean);
  const name = heading || clean(document.title.replace(/\s*-\s*Google Maps\s*$/i, ""));
  const full = parts.join("\n\n");
  const link = (text) => issueLink({
    template: "add-venue-from-link.yml", title: "[New Venue]: " + name, maps_link: location.href, menu_text: text,
  });
  const text = fit(full, link);
  const cut = text !== full;
  let copied = false;
  try { await navigator.clipboard.writeText(full); copied = true; } catch (e) { /* the browser may refuse; the form still has the text */ }
  if (cut) {
    alert("Date Nite: the menu has " + total + " dishes, too many for one link, so the form holds the first " +
          text.split("\n").filter((l) => / \$/.test(l)).length + ". " +
          (copied ? "The whole menu is on your clipboard: paste it over the Menu text box before you press Create."
                  : "Your browser would not copy the rest; the form still has enough to price the venue."));
  }
  if (win) win.location.href = link(text); else location.href = link(text);

  // ---- Uber Eats and Postmates ----
  async function delivery() {
    const DOLLARS = /^\$\s?(\d{1,3}(?:\.\d{2})?)(?!\d)/;
    const PRESELECT = /drink|beverage|bebida|refresco|soda|agua|jugo|juice|cocktail|margarita|cerveza|beer|wine|vino|dessert|postre|sweet|dulce/i;
    const money = (v) => { const n = Number(v); return Number.isFinite(n) && n > 0 && n < 500 ? "$" + n.toFixed(2) : null; };
    const list = (v) => (Array.isArray(v) ? v : v ? [v] : []);
    let name = "", town = "", state = "";
    const sections = []; // [{name, lines: ["Dish  $9.00"]}]
    const add = (section, dish, price) => {
      section = clean(section) || "Menu";
      dish = clean(dish).slice(0, 70);
      if (!dish || !price) return;
      let s = sections.find((x) => x.name === section);
      if (!s) sections.push((s = { name: section, lines: [] }));
      const line = dish + "  " + price;
      if (!s.lines.includes(line)) s.lines.push(line);
    };

    // 1. The page's own description of the restaurant (schema.org data), when it carries the menu
    for (const tag of document.querySelectorAll('script[type="application/ld+json"]')) {
      let data;
      try { data = JSON.parse(tag.textContent); } catch (e) { continue; }
      for (const item of list(data).concat(list(data && data["@graph"]))) {
        if (!item || typeof item !== "object") continue;
        if (item.name && item.address && !name) {
          name = clean(item.name);
          town = clean(item.address.addressLocality);
          state = clean(item.address.addressRegion);
        }
        for (const menu of list(item.hasMenu)) {
          for (const sec of list(menu && menu.hasMenuSection)) {
            for (const dish of list(sec && sec.hasMenuItem)) {
              const offer = list(dish && dish.offers)[0] || {};
              add(sec.name, dish && dish.name, money(offer.price));
            }
          }
        }
      }
    }

    // 2. Otherwise read the page as drawn: scroll it so every dish loads, then take each price with the dish name
    // beside it and the nearest heading above it as the section
    if (!sections.length) {
      for (let y = 0, i = 0; i < 80 && y < document.documentElement.scrollHeight; i++) {
        y += window.innerHeight * 0.9;
        window.scrollTo(0, y);
        await sleep(250);
        readDrawn();
      }
      window.scrollTo(0, 0);
    }
    function readDrawn() {
      const count = (el) => (el.innerText.match(/\$\s?\d{1,3}(?:\.\d{2})?/g) || []).length;
      let section = "";
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (node.nodeType === 1) {
          if (/^H[2-4]$/.test(node.tagName) && node.getClientRects().length && clean(node.innerText).length < 60) {
            // A heading inside a dish's own box (a box with exactly one price) is the dish's name, not a section
            let box = node.parentElement;
            while (box && box !== document.body && count(box) === 0) box = box.parentElement;
            if (!(box && box !== document.body && count(box) === 1)) section = clean(node.innerText);
          }
          continue;
        }
        const m = clean(node.nodeValue).match(DOLLARS);
        if (!m || !node.parentElement || !node.parentElement.getClientRects().length) continue;
        let row = node.parentElement;
        while (row.parentElement && row.parentElement !== document.body && count(row.parentElement) === 1) row = row.parentElement;
        const dish = row.innerText.split("\n").map(clean).find((l) => l && !DOLLARS.test(l));
        add(section, dish, money(m[1]));
      }
    }

    const heading = [...document.querySelectorAll("h1")].map((h) => clean(h.innerText)).find(Boolean);
    name = (name || heading || clean(document.title.split(/[|\-–]/)[0])).replace(/\s*\([^)]*\)\s*$/, "");
    const total = sections.reduce((n, s) => n + s.lines.length, 0);
    if (!total) {
      alert("Date Nite: no dishes with prices were found on this page. Open the restaurant's own page on the site and try again.");
      return;
    }

    // 3. A small panel: which sections to send, and the venue's name and town for the Edit a Venue form
    const el = (tag, style, text) => {
      const e = document.createElement(tag);
      if (style) e.style.cssText = style;
      if (text) e.textContent = text;
      return e;
    };
    const old = document.getElementById("datenite-panel");
    if (old) old.remove();
    const panel = el("div", "position:fixed;top:12px;right:12px;z-index:2147483647;width:340px;max-height:90vh;overflow:auto;" +
      "background:#fff;color:#222;border:1px solid #888;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.3);" +
      "padding:14px;font:14px/1.4 system-ui,sans-serif;text-align:left");
    panel.id = "datenite-panel";
    panel.appendChild(el("div", "font-weight:700;font-size:16px;margin-bottom:6px", "Date Nite: send menu sections"));
    panel.appendChild(el("div", "color:#555;margin-bottom:8px",
      "Only the ticked sections are sent. Saved prices change only for the kinds of dish they cover. Delivery prices can run higher than in-store prices."));
    const inputs = {};
    for (const [key, labelText, value] of [["name", "Restaurant", name], ["town", "Town", town], ["state", "State", state]]) {
      const row = el("label", "display:block;margin:4px 0", labelText + " ");
      const input = el("input", "width:" + (key === "state" ? "60px" : "220px") + ";padding:3px;border:1px solid #999;border-radius:4px;font:inherit;color:#222;background:#fff");
      input.value = value || "";
      row.appendChild(input);
      panel.appendChild(row);
      inputs[key] = input;
    }
    const boxes = [];
    const listBox = el("div", "margin:8px 0;border-top:1px solid #ddd;padding-top:6px");
    for (const s of sections) {
      const row = el("label", "display:block;margin:2px 0;cursor:pointer");
      const box = el("input");
      box.type = "checkbox";
      box.checked = PRESELECT.test(s.name);
      row.appendChild(box);
      row.appendChild(document.createTextNode(" " + s.name + " (" + s.lines.length + ")"));
      listBox.appendChild(row);
      boxes.push(box);
    }
    panel.appendChild(listBox);
    const status = el("div", "margin:6px 0;font-weight:600");
    panel.appendChild(status);
    const go = el("button", "padding:7px 12px;border:0;border-radius:6px;background:#1a73e8;color:#fff;font:inherit;font-weight:600;cursor:pointer;margin-right:8px", "Open the Edit a Venue form");
    const cancel = el("button", "padding:7px 12px;border:1px solid #999;border-radius:6px;background:#fff;color:#222;font:inherit;cursor:pointer", "Cancel");
    panel.appendChild(go);
    panel.appendChild(cancel);
    document.body.appendChild(panel);

    const chosen = () => sections.filter((s, i) => boxes[i].checked);
    const refresh = () => {
      const n = chosen().reduce((sum, s) => sum + s.lines.length, 0);
      const ok = n >= 6 && clean(inputs.name.value) && clean(inputs.town.value) && clean(inputs.state.value);
      status.textContent = n + " prices ticked" + (n < 6 ? " (the importer needs at least 6; tick another section)" :
        ok ? "" : " (fill in the restaurant, town and state)");
      status.style.color = ok ? "#137333" : "#b3261e";
      go.disabled = !ok;
      go.style.opacity = ok ? "1" : ".5";
    };
    panel.addEventListener("input", refresh);
    panel.addEventListener("change", refresh);
    refresh();
    cancel.addEventListener("click", () => panel.remove());
    go.addEventListener("click", async () => {
      const full = chosen().map((s) => "=== " + s.name + " ===\n" + s.lines.join("\n")).join("\n\n");
      const venue = clean(inputs.name.value);
      const link = (text) => issueLink({
        template: "edit-venue.yml", title: "[Edit Venue]: " + venue, venue_name: venue,
        town: clean(inputs.town.value), state: clean(inputs.state.value), menu_text: text,
      });
      const text = fit(full, link);
      // Opened first, while the click still counts, so the browser does not block it as a pop-up
      const win = window.open(link(text), "_blank");
      if (!win) location.href = link(text);
      if (text !== full) {
        let copied = false;
        try { await navigator.clipboard.writeText(full); copied = true; } catch (e) { /* the form still has the first part */ }
        alert("Date Nite: the ticked sections were too long for one link, so the form holds the first part. " +
              (copied ? "The whole text is on your clipboard: paste it over the Menu text box before you press Create."
                      : "Tick fewer sections, or paste the rest into the Menu text box."));
      }
      panel.remove();
    });
  }
})();
