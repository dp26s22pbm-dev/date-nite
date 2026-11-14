(async () => {
  // Date Nite: send the menu on a Google Maps listing to the importer.
  // Run it from a restaurant's page on google.com/maps while signed in to Google. It opens the listing's Menu tab,
  // reads every dish and price in each section, and opens the "Add New Venue (from a Google Maps link)" issue form
  // with the Maps link and the menu filled in. Nothing is sent until you press "Create" on that form.
  // Installed from maps-menu-bookmarklet.html, which builds the bookmark from this file.
  const REPO = "dp26s22pbm-dev/date-nite";
  const SKIP = ["overview", "photo", "breakfast", "lunch", "brunch", "happy", "kid", "catering", "party", "private", "gift"];
  const URL_BUDGET = 7000; // GitHub refuses very long links, so the menu is kept as "Dish  $price" lines
  const PRICE = /^\$\s?\d{1,3}(?:\.\d{2})?$/;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clean = (t) => (t || "").replace(/\s+/g, " ").trim();

  if (!/(^|\.)google\.[a-z.]+$/.test(location.hostname) || !location.pathname.startsWith("/maps")) {
    alert("Date Nite: open the restaurant's page in Google Maps first, then click this bookmark.");
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
  const link = (text) => "https://github.com/" + REPO + "/issues/new?" + new URLSearchParams({
    template: "add-venue-from-link.yml", title: "[New Venue]: " + name, maps_link: location.href, menu_text: text,
  }).toString();
  let text = full;
  let lines = full.split("\n");
  while (link(text).length > URL_BUDGET && lines.length > 6) {
    lines = lines.slice(0, Math.floor(lines.length * 0.9));
    text = lines.join("\n");
  }
  const cut = text !== full;
  let copied = false;
  try { await navigator.clipboard.writeText(full); copied = true; } catch (e) { /* the browser may refuse; the form still has the text */ }
  if (cut) {
    alert("Date Nite: the menu has " + total + " dishes, too many for one link, so the form holds the first " +
          lines.filter((l) => / \$/.test(l)).length + ". " +
          (copied ? "The whole menu is on your clipboard: paste it over the Menu text box before you press Create."
                  : "Your browser would not copy the rest; the form still has enough to price the venue."));
  }
  if (win) win.location.href = link(text); else location.href = link(text);
})();
