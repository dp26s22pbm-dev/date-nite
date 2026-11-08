const cases = Array.isArray(input.cases) ? input.cases.map(item => item && item.result ? item.result : item) : [];

// Tunable gates. A case that fails a gate can never be recommended, whatever its score.
const REQUIRE_IL_HQ = true;
const ALLOWED_SIZES = ['middle_market'];
const MIN_SCORE = 3;
const RESEARCH_LIMIT = 5;
const OUTREACH_LIMIT = 3;

function normalizeYesNo(value) {
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  const normalized = String(value ?? '').trim().toLowerCase();
  if (['yes', 'true', 'y', '1'].includes(normalized)) return 'yes';
  if (['no', 'false', 'n', '0'].includes(normalized)) return 'no';
  return 'unknown';
}

function isUnknown(value) {
  const normalized = String(value ?? '').trim().toLowerCase();
  return normalized === '' || normalized === 'unknown' || normalized === 'not listed' || normalized === 'n/a';
}

function normalizeSizeBand(value) {
  const normalized = String(value ?? '').trim().toLowerCase();
  if (isUnknown(normalized)) return 'unknown';
  if (normalized === 'micro' || /micro|under 50|less than 50|1-49|1 to 49/.test(normalized)) return 'micro';
  if (normalized === 'small') return 'small';
  if (normalized === 'middle_market' || /mid.?market|mid.?size|\b(50|51)\s*(-|to)\s*(200|500)|201\s*(-|to)\s*500/.test(normalized)) return 'middle_market';
  if (normalized === 'national' || /national|publicly traded|enterprise|fortune/.test(normalized)) return 'national';
  if (normalized === 'large' || /large|1,?001\+|over 1,?000|more than 1,?000/.test(normalized)) return 'large';
  return 'unknown';
}

function normalizeOwnership(value) {
  const n = String(value ?? '').trim().toLowerCase();
  if (isUnknown(n)) return 'unknown';
  if (/individual|natural person/.test(n)) return 'individual';
  if (/government|municipal|public sector|state agency|county|city of|village of|federal|school district|park district|public (university|agency|entity|body|authority)|state universit/.test(n)) return 'government';
  if (/publicly|public company|public corporation|^public$|nyse|nasdaq|exchange.?listed/.test(n)) return 'public';
  if (/non.?profit|not.for.profit|501\(c\)/.test(n)) return 'nonprofit';
  if (/private|llc|family|equity|franchise|closely held/.test(n)) return 'private';
  return 'other';
}

function normalizeState(value) {
  const n = String(value ?? '').trim().toLowerCase().replace(/\./g, '');
  if (isUnknown(n)) return '';
  if (n === 'il' || n === 'ill' || n === 'illinois') return 'IL';
  return n.toUpperCase();
}

// Employee count read from the size evidence. The model's band is only trusted when no count is found.
function employeeCount(text) {
  const t = String(text || '').toLowerCase().replace(/(\d),(\d)/g, '$1$2');
  const unit = '(?:full[- ]time |total |paid |regular )?(?:employees|employee|staff|people|workers|team members)';
  const counts = [];
  // "fewer than 50" is an upper bound (49 or less); "more than 450" is a lower bound (451 or more).
  const bounded = (n, pos) => {
    const before = t.slice(Math.max(0, pos - 20), pos);
    if (/(fewer than|less than|under|below|up to|no more than|at most)\s*$/.test(before)) return n - 1;
    if (/(more than|over|at least|exceeds?|above)\s*$/.test(before)) return n + 1;
    return n;
  };
  for (const m of t.matchAll(new RegExp('(\\d+)\\s*(?:-|\u2013|to)\\s*(\\d+)\\+?\\s*' + unit, 'g'))) counts.push(Number(m[2]));
  for (const m of t.matchAll(new RegExp('(\\d+)\\s*\\+?\\s*' + unit, 'g'))) counts.push(bounded(Number(m[1]), m.index));
  for (const m of t.matchAll(new RegExp(unit + '[^\\d]{0,25}?(\\d+)', 'g'))) {
    const n = Number(m[1]);
    if (n < 1990 || n > 2039) counts.push(bounded(n, m.index + m[0].length - m[1].length));
  }
  return counts.length ? Math.max(...counts) : null;
}

function bandFromCount(n) {
  if (n < 50) return 'micro';
  if (n <= 1000) return 'middle_market';
  if (n <= 10000) return 'large';
  return 'national';
}

function scoreCase(raw) {
  const c = {
    ...raw,
    hasIllinoisOperations: normalizeYesNo(raw.hasIllinoisOperations),
    isClassAction: normalizeYesNo(raw.isClassAction),
    sizeBand: normalizeSizeBand(raw.sizeBand)
  };
  const flags = [];
  const hq = normalizeState(c.hqState);
  const ops = c.hasIllinoisOperations;
  const count = employeeCount(c.sizeEvidence);
  if (count !== null) {
    c.sizeBand = bandFromCount(count);
  } else if (c.sizeBand === 'middle_market') {
    c.sizeBand = 'unknown';
    flags.push('Employee count not found');
  }
  const size = c.sizeBand;
  const ownershipType = normalizeOwnership(c.ownership);
  const cls = c.isClassAction;
  const confidence = String(c.confidence || '').trim().toLowerCase();
  const sizeEvidence = String(c.sizeEvidence || '').trim().toLowerCase();
  const notes = String(c.notes || '').trim().toLowerCase();
  const nature = String(c.natureOfSuit || '').toLowerCase();
  const profileFailed = confidence === 'research failed' || sizeEvidence === 'research failed' || notes === 'research failed';
  // An unverified HQ or size can no longer pass: unknown used to score 0 and still qualify.
  const insufficientProfile = profileFailed || !hq || size === 'unknown';
  let score = 0;

  if (hq === 'IL') score += 4;
  else if (hq) {
    score -= 2;
    flags.push('Out-of-state HQ');
    if (ops === 'yes') score += 2;
  } else {
    flags.push('HQ unknown');
  }

  if (size === 'middle_market') score += 2;
  else if (size === 'large') score -= 1;
  else if (size === 'national') {
    score -= 4;
    flags.push('National/public');
  } else if (size === 'unknown') {
    flags.push('Size unknown');
  }

  if (cls === 'yes') {
    score -= 2;
    flags.push('Class action');
  } else if (cls === 'unknown') {
    flags.push('Class status unknown');
  }

  if (cls !== 'yes' && /(single.?plaintiff|collective)/i.test(nature)) score += 1;
  if (confidence === 'low') {
    score -= 1;
    flags.push('Low confidence');
  }

  const reasons = [];
  if (ownershipType === 'government') reasons.push('Government ownership');
  if (ownershipType === 'individual') reasons.push('Individual defendant');
  if (ownershipType === 'public') reasons.push('Publicly traded');
  if (size === 'micro') reasons.push('Micro business');
  else if (size === 'large' || size === 'national') reasons.push(`Too large (${size})`);
  else if (size !== 'unknown' && !ALLOWED_SIZES.includes(size)) reasons.push(`Size outside target (${size})`);
  if (REQUIRE_IL_HQ && hq && hq !== 'IL') reasons.push(`Out-of-state HQ (${hq})`);
  const disqualified = reasons.join(' / ');

  if (disqualified) {
    flags.splice(0, flags.length, `Disqualified: ${disqualified}`);
  }
  if (profileFailed) flags.push('Research failed and review manually');
  if (insufficientProfile) flags.push('Insufficient profile');

  const why = disqualified
    ? `Not recommended: ${disqualified}. HQ ${hq || 'unknown'}; ${size}.`
    : `HQ ${hq || 'unknown'}; ${size}; ${cls === 'yes' ? 'class action' : cls === 'no' ? 'not class action' : 'class status unknown'}. Flags: ${flags.length ? flags.join(', ') : 'none'}.`;
  return {
    ...c,
    Score: score,
    Flags: flags.join(', '),
    recommendOutreach: 'No',
    whyRecommended: why,
    researchCandidate: false,
    _disqualified: Boolean(disqualified),
    _profileFailed: profileFailed,
    _insufficientProfile: insufficientProfile
  };
}

const scored = cases.map(scoreCase);
const eligible = scored
  .filter(c => !c._disqualified && !c._profileFailed && !c._insufficientProfile && Number(c.Score) >= MIN_SCORE)
  .sort((a, b) => Number(b.Score) - Number(a.Score) || String(a.caseInitiatedDate || '').localeCompare(String(b.caseInitiatedDate || '')));

const topFive = eligible.slice(0, RESEARCH_LIMIT);
for (const candidate of topFive) candidate.researchCandidate = true;
for (const candidate of eligible.slice(0, OUTREACH_LIMIT)) candidate.recommendOutreach = 'Yes';

for (const c of scored) {
  delete c._disqualified;
  delete c._profileFailed;
  delete c._insufficientProfile;
}

// Module 39 iterates this list, so it must never be empty or the scenario stops before the sheet writes.
const researchFeed = topFive.length
  ? topFive
  : [{ docketNumber: 'NO-CANDIDATES', corporateDefendant: 'NO-CANDIDATES', resolvedEntity: '', parentCompany: '', hqState: '', researchCandidate: false }];

return { cases: scored, researchCases: topFive, researchFeed };
