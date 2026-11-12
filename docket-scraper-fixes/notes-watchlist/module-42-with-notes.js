const rawDefendant = String(input.defendant || '');
const clients = Array.isArray(input.clients) ? input.clients : [];
const history = Array.isArray(input.history) ? input.history : [];

function normalizeWords(value) {
  let text = String(value || '').toLowerCase();

  // Treat ampersand as the word "and" before punctuation cleanup.
  text = text.replace(/&/g, ' and ');

  // Strip punctuation into spaces, then collapse dotted abbreviations
  // such as L.L.C. and L.P. before removing legal suffixes.
  text = text
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\b(l\s+l\s+c|l\s+p|i\s+n\s+c)\b/g, value => value.replace(/\s/g, ''))
    .replace(/\s+/g, ' ')
    .trim();

  // Remove legal suffixes after punctuation has been stripped.
  text = text.replace(
    /\b(incorporated|inc|llc|lp|llp|corp|corporation|co|company|ltd)\b/g,
    ' '
  );

  return text.replace(/\s+/g, ' ').trim();
}

function compact(value) {
  return normalizeWords(value).replace(/\s/g, '');
}

function distance(a, b) {
  const prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(
        cur[j - 1] + 1,
        prev[j] + 1,
        prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1)
      );
    }
    for (let j = 0; j <= b.length; j++) prev[j] = cur[j];
  }
  return prev[b.length];
}

function wholeWordContained(longer, shorter) {
  if (!longer || !shorter || shorter.length < 4) return false;
  const escaped = shorter.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\\ /g, '\\s+');
  return new RegExp(`(^|\\s)${escaped}(?=$|\\s)`).test(longer);
}

const defendants = rawDefendant
  .split(';')
  .map(value => ({ raw: value.trim(), normalized: normalizeWords(value) }))
  .filter(value => value.normalized);

const references = [];
for (const row of clients) {
  if (!row || typeof row !== 'object') continue;
  const clientName = row['0'] == null ? '' : String(row['0']).trim();
  const affiliateCell = row['1'] == null ? '' : String(row['1']);
  const rawNames = [clientName, ...affiliateCell.split(/[;\r\n]+/)];
  for (const rawName of rawNames) {
    const original = rawName.trim();
    const normalized = normalizeWords(original);
    if (normalized && compact(normalized).length >= 4) {
      references.push({ original, normalized, compact: compact(normalized) });
    }
  }
}

const matches = [];
for (const defendant of defendants) {
  const defendantCompact = compact(defendant.normalized);
  for (const reference of references) {
    if (defendant.normalized === reference.normalized || defendantCompact === reference.compact) {
      matches.push({ reference: reference.original, matchType: 'exact' });
      continue;
    }
    if (wholeWordContained(defendant.normalized, reference.normalized) || wholeWordContained(reference.normalized, defendant.normalized)) {
      matches.push({ reference: reference.original, matchType: 'contained' });
      continue;
    }
    if (defendantCompact.length >= 6 && reference.compact.length >= 6) {
      const maxLength = Math.max(defendantCompact.length, reference.compact.length);
      const threshold = Math.max(2, Math.ceil(maxLength * 0.25));
      if (distance(defendantCompact, reference.compact) <= threshold) {
        matches.push({ reference: reference.original, matchType: 'near' });
      }
    }
  }
}

// Tracker notes: employers already marked as firm clients, represented, or not in Illinois.
const historyHits = [];
for (const row of history) {
  if (!row || typeof row !== 'object') continue;
  const name = row['0'] == null ? '' : String(row['0']).trim();
  const docket = row['1'] == null ? '' : String(row['1']).trim();
  const adClient = row['2'] == null ? '' : String(row['2']).trim().toLowerCase();
  const note = row['3'] == null ? '' : String(row['3']).replace(/\s+/g, ' ').trim();
  if (!name || name === '(none)') continue;
  if (docket && docket === String(input.case_number || '').trim()) continue;
  if (adClient !== 'yes' && !note) continue;
  for (const rawName of name.split(';')) {
    const ref = normalizeWords(rawName);
    if (!ref || compact(ref).length < 4) continue;
    for (const defendant of defendants) {
      const exact = defendant.normalized === ref || compact(defendant.normalized) === compact(ref);
      if (exact || wholeWordContained(defendant.normalized, ref) || wholeWordContained(ref, defendant.normalized)) {
        historyHits.push({
          reference: name,
          matchType: exact ? 'note exact' : 'note contained',
          note: adClient === 'yes' ? 'AD Client' + (note ? ': ' + note : '') : note
        });
      }
    }
  }
}

const bestMatch = matches.find(match => match.matchType === 'exact') || matches.find(match => match.matchType === 'contained') || matches.find(match => match.matchType === 'near');

const historyMatch = historyHits[0];

return {
  case_number: input.case_number,
  case_title: input.case_title,
  filing_date: input.filing_date,
  court: input.court,
  plaintiff: input.plaintiff,
  defendant: input.defendant,
  cause_of_action: input.cause_of_action,
  is_class_action: input.is_class_action,
  conflict: Boolean(bestMatch) || Boolean(historyMatch),
  conflictMatch: bestMatch ? bestMatch.reference : (historyMatch ? historyMatch.reference + ' | ' + historyMatch.note.slice(0, 120) : ''),
  matchType: bestMatch ? bestMatch.matchType : (historyMatch ? historyMatch.matchType : '')
};
