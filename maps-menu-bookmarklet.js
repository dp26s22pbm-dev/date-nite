(async () => {
  // Date Nite: send a menu to the importer from your own browser.
  // On a restaurant's page in Google Maps (signed in to Google) it opens the listing's Menu tab, reads every dish and
  // price in each section, and opens the "Add New Venue (from a Google Maps link)" issue form with the Maps link and
  // the menu filled in.
  // On any other page (the restaurant's own website, Uber Eats, Postmates, another ordering site) it reads the dishes
  // and prices on the page and shows a small panel of the menu's sections. Tick the ones to send and it opens the
  // "Edit a Venue" form (which changes only the price categories the text covers) or, for a venue not yet on the map,
  // the "Add New Venue" form. For a venue held in Venue Review it copies the menu and opens that venue's review
  // issue, where the menu is pasted as a comment. Sections can be kept while you open another page of the same site (its dessert or
  // drinks page) so one form holds them all. On Uber Eats and Postmates only drinks and desserts are ticked to start,
  // because delivery prices for the mains are often marked up.
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
  const DELIVERY = /(^|\.)(ubereats|postmates|doordash|grubhub|seamless)\.com$/;
  if (!onMaps) {
    if (!/^https?:$/.test(location.protocol)) {
      alert("Date Nite: open a restaurant's menu page (its website, Google Maps, or an ordering site) first, then click this bookmark.");
      return;
    }
    return anySite(DELIVERY.test(location.hostname));
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

  // ---- Every other site: the restaurant's own website, Uber Eats, Postmates, other ordering pages ----
  async function anySite(delivery) {
    const DOLLARS = /^\$\s?(\d{1,3}(?:\.\d{2})?)(?!\d)/;
    const DRINKS_DESSERTS = /drink|beverage|bebida|refresco|soda|agua|jugo|juice|cocktail|margarita|cerveza|beer|wine|vino|dessert|postre|sweet|dulce/i;
    const NOT_DINNER = /breakfast|lunch|brunch|happy|kid|catering|banquet|party|private|gift|merch/i;
    const BASKET = "datenite-basket";
    const money = (v) => { const n = Number(v); return Number.isFinite(n) && n > 0 && n < 500 ? "$" + n.toFixed(2) : null; };
    const list = (v) => (Array.isArray(v) ? v : v ? [v] : []);
    const old = document.getElementById("datenite-panel");
    if (old) old.remove();
    let name = "", town = "", state = "";
    const sections = []; // [{name, lines: ["Dish  $9.00"], saved: true when kept from an earlier page}]
    const add = (section, dish, price) => {
      section = clean(section).slice(0, 60) || "Menu";
      dish = clean(dish).replace(/[\s.·…_\-–:|]+$/, "").slice(0, 70);
      if (!dish || !price) return;
      let s = sections.find((x) => x.name === section && !x.saved);
      if (!s) sections.push((s = { name: section, lines: [] }));
      const line = dish + "  " + price;
      if (!s.lines.includes(line)) s.lines.push(line);
    };

    // Two candidate groupings of the dishes read from the page as drawn (see readBoxes)
    const byHeading = [], byLine = [];
    const put = (target, section, dish, price) => {
      section = clean(section).slice(0, 60) || "Menu";
      let s = target.find((x) => x.name === section);
      if (!s) target.push((s = { name: section, lines: [] }));
      const line = dish + "  " + price;
      if (!target.some((x) => x.lines.includes(line))) s.lines.push(line);
    };

    // 1. The page's own description of the restaurant (schema.org data), when it carries the menu
    for (const tag of document.querySelectorAll('script[type="application/ld+json"]')) {
      let data;
      try { data = JSON.parse(tag.textContent); } catch (e) { continue; }
      for (const item of list(data).concat(list(data && data["@graph"]))) {
        if (!item || typeof item !== "object") continue;
        if (item.name && item.address && typeof item.address === "object" && !name) {
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

    // 2. Otherwise read the page as drawn. Scroll it first so every dish loads.
    if (!sections.length) {
      for (let y = 0, i = 0; i < 80 && y < document.documentElement.scrollHeight; i++) {
        y += window.innerHeight * 0.9;
        window.scrollTo(0, y);
        await sleep(delivery ? 250 : 120);
        if (delivery) readBoxes(); // ordering sites draw only the dishes near the screen, so read at each step
      }
      window.scrollTo(0, 0);
      if (!delivery) readBoxes();
      pickSections();
      if (!delivery && count() < 6) { sections.length = 0; readLines(); }
    }
    function count() { return sections.reduce((n, s) => n + s.lines.length, 0); }

    // 2a. Each "$" price with the dish name in the same box. The section is worked out two ways, because sites mark
    // their categories differently: the nearest heading tag above the dish, and the short line of text sitting just
    // above a run of dishes. pickSections() keeps whichever gave the more sensible grouping.
    function readBoxes() {
      const prices = (el) => (el.innerText.match(/\$\s?\d{1,3}(?:\.\d{2})?/g) || []).length;
      const found = []; // {dish, price, heading}
      let section = "";
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (node.nodeType === 1) {
          if (/^H[2-5]$/.test(node.tagName) && node.getClientRects().length && clean(node.innerText).length < 60) {
            // A heading inside a dish's own box (a box with exactly one price) is the dish's name, not a section
            let box = node.parentElement;
            while (box && box !== document.body && prices(box) === 0) box = box.parentElement;
            if (!(box && box !== document.body && prices(box) === 1)) section = clean(node.innerText);
          }
          continue;
        }
        const m = clean(node.nodeValue).match(DOLLARS);
        if (!m || !node.parentElement || !node.parentElement.getClientRects().length) continue;
        let row = node.parentElement;
        while (row.parentElement && row.parentElement !== document.body && prices(row.parentElement) === 1) row = row.parentElement;
        let dish = row.innerText.split("\n").map(clean).find((l) => l && !DOLLARS.test(l));
        dish = clean(dish).replace(/[\s.·…_\-–:|]+$/, "");
        if (dish && money(m[1])) found.push({ dish, price: money(m[1]), heading: section });
      }
      // The short line just above a run of dishes: not a dish, no price, and the next line is a dish's name
      const names = new Set(found.map((f) => f.dish));
      const lines = document.body.innerText.split("\n").map(clean).filter(Boolean);
      const label = {};
      let current = "", candidate = "", sawPrice = false, afterPrice = false;
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (names.has(line)) {
          if (candidate) current = candidate;
          candidate = "";
          afterPrice = false;
          if (!(line in label)) label[line] = current;
          continue;
        }
        if (/\$\s?\d/.test(line)) { sawPrice = afterPrice = true; candidate = ""; continue; }
        if (line.length > 50) continue;
        // After a dish's price, the first short line before the next dish is the next category's name (a longer note
        // may follow it). Before any price, it is the line right above the first dish.
        if (afterPrice ? !candidate : !sawPrice && names.has(lines[i + 1])) candidate = line;
      }
      for (const f of found) {
        put(byHeading, f.heading, f.dish.slice(0, 70), f.price);
        put(byLine, label[f.dish] || "", f.dish.slice(0, 70), f.price);
      }
    }
    function pickSections() {
      // A sensible grouping has more than one section and a few dishes in each
      const score = (t) => {
        const n = t.reduce((sum, x) => sum + x.lines.length, 0);
        return t.length > 1 && n / t.length >= 2.5 ? t.length : 0;
      };
      const best = score(byLine) > score(byHeading) ? byLine : score(byHeading) ? byHeading : byLine.length ? [
        { name: "Menu", lines: [].concat(...byHeading.map((x) => x.lines)) }] : [];
      for (const x of best) if (x.lines.length) sections.push({ name: x.name, lines: x.lines });
    }

    // 2b. For menus laid out as plain lines, often with no "$": a price at the end of a dish's line, or on a line of
    // its own under the dish's name
    function readLines() {
      const headings = new Set([...document.querySelectorAll("h1,h2,h3,h4,h5,h6,strong,b")]
        .map((h) => clean(h.innerText)).filter((t) => t && t.length < 60));
      const lines = document.body.innerText.split("\n").map(clean).filter(Boolean);
      const END = /^(.*?[A-Za-z)].*?)[\s.·…_\-–:|]+\$?\s?(\d{1,3}(?:\.\d{2})?)(?:\s*[\/|,-]\s*\$?\d{1,3}(?:\.\d{2})?)*$/;
      // A dish and its price on one line: it has a "$", cents, or a run of dots or spaces leading to the number
      const endDish = (line) => {
        const m = line.length <= 160 && line.match(END);
        return m && (/\$/.test(line) || /\.\d{2}$/.test(line) || /[\s.·…_\-–:|]{2,}\d{1,3}$/.test(line)) ? m : null;
      };
      const ALONE = /^\$?\s?(\d{1,3}(?:\.\d{2})?)(?:\s*[\/|,-]\s*\$?\d{1,3}(?:\.\d{2})?)*$/;
      let section = "";
      let pending = [];
      const settle = (dish) => {
        const above = pending.filter((h) => h !== dish);
        if (above.length) section = above[above.length - 1];
        pending = [];
      };
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        let m = line.match(ALONE);
        if (m) {
          const n = Number(m[1]);
          if (!/\$|\.\d{2}/.test(line) && (n < 2 || n >= 200)) continue; // a bare number that is not a price
          // The dish's name is a heading among the few lines above, else the nearest short line (past a long
          // description)
          let dish = "", near = "";
          for (let j = i - 1; j >= Math.max(0, i - 3); j--) {
            if (ALONE.test(lines[j]) || endDish(lines[j])) break;
            if (headings.has(lines[j])) { dish = lines[j]; break; }
            if (!near && lines[j].length <= 60) near = lines[j];
          }
          dish = dish || near;
          if (dish) { settle(dish); add(section, dish, money(m[1])); }
          continue;
        }
        m = endDish(line);
        if (m) {
          settle(m[1]);
          add(section, m[1], money(m[2]));
          continue;
        }
        if (headings.has(line)) pending.push(line);
      }
    }

    // Sections kept from an earlier page of the same site
    let basket = null;
    try { basket = JSON.parse(localStorage.getItem(BASKET) || "null"); } catch (e) { basket = null; }
    const kept = basket && Array.isArray(basket.sections) ? basket.sections : [];
    for (const s of kept) sections.unshift({ name: s.name, lines: s.lines, saved: true });
    if (basket) { name = basket.name || name; town = basket.town || town; state = basket.state || state; }

    const shown = (tag) => [...document.querySelectorAll(tag)].filter((h) => h.getClientRects().length && h.offsetParent)
      .map((h) => clean(h.innerText)).find((t) => t && t.length < 60);
    const heading = shown("h1") || (delivery ? "" : shown("h2"));
    const siteName = (document.querySelector('meta[property="og:site_name"]') || {}).content;
    // From the page title, the longest part that is not a page name ("Menu | The Test Pub" gives "The Test Pub")
    const GENERIC = /^(home|menus?|our menus?|main menu|food|dinner|desserts?|drinks?|order( online)?|online ordering|food ordering|about|locations?)$/i;
    const titled = document.title.split(/\s[|\-–:]\s/).map(clean).filter((t) => t && !GENERIC.test(t)).sort((x, y) => y.length - x.length)[0];
    if (!name) name = clean(siteName) || (delivery ? heading || titled : titled || heading) || "";
    name = (name || "").replace(/\s*\([^)]*\)\s*$/, "");
    if (!town) {
      // "..., Joliet, IL 60435" anywhere on the page
      const where = document.body.innerText.match(/,\s*([A-Z][A-Za-z .'-]{2,30}),\s*([A-Z]{2})\s+\d{5}\b/);
      if (where) { town = clean(where[1]); state = where[2]; }
    }
    if (!count()) {
      alert("Date Nite: no dishes with prices were found on this page. If the menu is a PDF, a picture or sits inside " +
            "another site's frame, the bookmark cannot read it: copy the menu text into the Edit a Venue form by hand.");
      return;
    }

    // 3. A small panel: which sections to send, and the venue's details for the form
    const el = (tag, style, text) => {
      const e = document.createElement(tag);
      if (style) e.style.cssText = style;
      if (text) e.textContent = text;
      return e;
    };
    const FIELD = "padding:3px;border:1px solid #999;border-radius:4px;font:inherit;color:#222;background:#fff";
    const panel = el("div", "position:fixed;top:12px;right:12px;z-index:2147483647;width:350px;max-height:90vh;overflow:auto;" +
      "background:#fff;color:#222;border:1px solid #888;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.3);" +
      "padding:14px;font:14px/1.4 system-ui,sans-serif;text-align:left");
    panel.id = "datenite-panel";
    panel.appendChild(el("div", "font-weight:700;font-size:16px;margin-bottom:6px", "Date Nite: send menu sections"));
    panel.appendChild(el("div", "color:#555;margin-bottom:8px", "Only the ticked sections are sent." +
      (delivery ? " Delivery prices can run higher than in-store prices, so only drinks and desserts are ticked to start." : "")));
    const inputs = {};
    for (const [key, labelText, value] of [["name", "Restaurant", name], ["town", "Town", town], ["state", "State", state]]) {
      const row = el("label", "display:block;margin:4px 0", labelText + " ");
      const input = el("input", "width:" + (key === "state" ? "60px" : "220px") + ";" + FIELD);
      input.value = value || "";
      row.appendChild(input);
      panel.appendChild(row);
      inputs[key] = input;
    }
    const modeRow = el("label", "display:block;margin:4px 0", "Venue is ");
    const mode = el("select", FIELD);
    for (const [value, text] of [["edit", "already on the map (Edit a Venue)"], ["add", "new (Add New Venue)"],
                                 ["review", "in Venue Review (paste as a comment)"]]) {
      const option = el("option", "", text);
      option.value = value;
      mode.appendChild(option);
    }
    modeRow.appendChild(mode);
    panel.appendChild(modeRow);
    const boxes = [];
    const listBox = el("div", "margin:8px 0;border-top:1px solid #ddd;padding-top:6px");
    for (const s of sections) {
      const row = el("label", "display:block;margin:2px 0;cursor:pointer");
      const box = el("input");
      box.type = "checkbox";
      box.checked = s.saved || (delivery ? DRINKS_DESSERTS.test(s.name) : !NOT_DINNER.test(s.name));
      row.appendChild(box);
      row.appendChild(document.createTextNode(" " + s.name + " (" + s.lines.length + ")" + (s.saved ? " · kept from another page" : "")));
      row.title = s.lines.slice(0, 12).join("\n");
      listBox.appendChild(row);
      boxes.push(box);
    }
    panel.appendChild(listBox);
    const status = el("div", "margin:6px 0;font-weight:600");
    panel.appendChild(status);
    const BUTTON = "padding:7px 10px;border-radius:6px;font:inherit;cursor:pointer;margin:0 6px 6px 0;";
    const go = el("button", BUTTON + "border:0;background:#1a73e8;color:#fff;font-weight:600", "Open the form");
    const copyOnly = el("button", BUTTON + "border:1px solid #1a73e8;background:#fff;color:#1a73e8;font-weight:600", "Copy menu only");
    const keep = el("button", BUTTON + "border:1px solid #999;background:#fff;color:#222", "Keep ticked, add another page");
    const cancel = el("button", BUTTON + "border:1px solid #999;background:#fff;color:#222", "Cancel");
    panel.appendChild(go);
    panel.appendChild(copyOnly);
    panel.appendChild(keep);
    panel.appendChild(cancel);
    if (kept.length) {
      const forget = el("button", BUTTON + "border:0;background:none;color:#b3261e;text-decoration:underline", "Forget kept sections");
      forget.addEventListener("click", () => { try { localStorage.removeItem(BASKET); } catch (e) { /* nothing kept */ } panel.remove(); });
      panel.appendChild(forget);
    }
    panel.appendChild(el("div", "color:#555;font-size:12px", "Hover over a section to see its first dishes. " +
      "\"Keep ticked\" holds them while you open this site's dessert or drinks page and click the bookmark there."));
    document.body.appendChild(panel);

    let shownForCopy = false; // the menu is showing in a box for hand copying, because the clipboard was refused
    const chosen = () => sections.filter((s, i) => boxes[i].checked);
    const refresh = () => {
      const n = chosen().reduce((sum, s) => sum + s.lines.length, 0);
      // A venue held in review is found by its name alone; the two forms also need the town and state
      const review = mode.value === "review";
      const filled = clean(inputs.name.value) && (review || (clean(inputs.town.value) && clean(inputs.state.value)));
      const ok = n >= 6 && filled;
      go.textContent = shownForCopy ? "Find the review issue" : review ? "Copy menu, find the review issue" : "Open the form";
      status.textContent = n + " prices ticked" + (n < 6 ? " (a form needs at least 6; tick more, or keep these and add another page)" :
        filled ? "" : review ? " (fill in the restaurant)" : " (fill in the restaurant, town and state)");
      status.style.color = ok ? "#137333" : "#b3261e";
      go.disabled = !ok;
      go.style.opacity = ok ? "1" : ".5";
      keep.disabled = !n;
      keep.style.opacity = n ? "1" : ".5";
      copyOnly.disabled = !n;
      copyOnly.style.opacity = n ? "1" : ".5";
    };
    panel.addEventListener("input", refresh);
    panel.addEventListener("change", refresh);
    refresh();
    cancel.addEventListener("click", () => panel.remove());
    keep.addEventListener("click", () => {
      try {
        localStorage.setItem(BASKET, JSON.stringify({
          name: clean(inputs.name.value), town: clean(inputs.town.value), state: clean(inputs.state.value),
          sections: chosen().map((s) => ({ name: s.name, lines: s.lines })),
        }));
      } catch (e) {
        alert("Date Nite: this site does not let the bookmark keep anything between pages. Send each page as its own form.");
        return;
      }
      panel.remove();
    });
    // Only puts the ticked sections on the clipboard, for pasting later. Opens nothing and needs no restaurant name.
    copyOnly.addEventListener("click", async () => {
      const full = chosen().map((s) => "=== " + s.name + " ===\n" + s.lines.join("\n")).join("\n\n");
      try {
        await navigator.clipboard.writeText(full);
      } catch (e) {
        const box = el("textarea", "width:100%;height:160px;margin-top:8px;font:12px monospace;color:#222;background:#fff");
        box.value = full;
        box.readOnly = true;
        panel.appendChild(el("div", "margin-top:8px;color:#b3261e;font-weight:600",
          "Your browser would not copy the menu. Copy it from this box."));
        panel.appendChild(box);
        box.focus();
        box.select();
        return;
      }
      try { localStorage.removeItem(BASKET); } catch (e) { /* nothing kept */ }
      alert("Date Nite: the menu is on your clipboard.");
      panel.remove();
    });
    go.addEventListener("click", async () => {
      const full = chosen().map((s) => "=== " + s.name + " ===\n" + s.lines.join("\n")).join("\n\n");
      const venue = clean(inputs.name.value);
      const where = [clean(inputs.town.value), clean(inputs.state.value)];
      if (mode.value === "review") {
        // A venue held for review lives in its own `venue-review` issue, and a menu reaches it as a comment there.
        // GitHub cannot pre-fill a comment, so the menu goes on the clipboard and the issue is found by name.
        const words = venue.split(/[^A-Za-z0-9]+/).filter((w) => w.length >= 3).join(" ");
        const search = "https://github.com/" + REPO + "/issues?" + new URLSearchParams({
          q: "is:issue is:open label:venue-review in:title " + words }).toString();
        if (shownForCopy) { window.open(search, "_blank"); panel.remove(); return; }
        let copied = false;
        // Copied before the new tab opens: a page may only write to the clipboard while it is the one in front
        try { await navigator.clipboard.writeText(full); copied = true; } catch (e) { /* shown for hand copying below */ }
        if (!copied) {
          const box = el("textarea", "width:100%;height:160px;margin-top:8px;font:12px monospace;color:#222;background:#fff");
          box.value = full;
          box.readOnly = true;
          panel.appendChild(el("div", "margin-top:8px;color:#b3261e;font-weight:600",
            "Your browser would not copy the menu. Copy it from this box, then press the button again or open the review issue yourself."));
          panel.appendChild(box);
          box.focus();
          box.select();
          shownForCopy = true;
          refresh();
          return;
        }
        const win = window.open(search, "_blank");
        try { localStorage.removeItem(BASKET); } catch (e) { /* nothing kept */ }
        alert("Date Nite: the menu is on your clipboard. Open the venue's review issue" +
              (win ? " in the tab that just opened" : " (your browser blocked the new tab; open the repository's venue-review issues)") +
              ", paste it as a comment and press Comment. The prices in the issue are updated from it; add the approve-venue label to put the venue on the map.");
        panel.remove();
        return;
      }
      const link = (text) => issueLink(mode.value === "edit"
        ? { template: "edit-venue.yml", title: "[Edit Venue]: " + venue, venue_name: venue, town: where[0], state: where[1], menu_text: text }
        : { template: "add-venue.yml", title: "[New Venue]: " + venue, venue_name: venue, address: where.join(", "),
            website: delivery ? "" : location.origin, menu_text: text });
      const text = fit(full, link);
      // Opened first, while the click still counts, so the browser does not block it as a pop-up
      const win = window.open(link(text), "_blank");
      if (!win) location.href = link(text);
      try { localStorage.removeItem(BASKET); } catch (e) { /* nothing kept */ }
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
