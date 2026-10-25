const allCases = Array.isArray(input.allCases) ? input.allCases : [];
const researchCases = Array.isArray(input.researchCases) ? input.researchCases : [];
const researchResults = Array.isArray(input.researchResults) ? input.researchResults : [];

// Keep these in step with module 38.
const MIN_SCORE = 3;
const OUTREACH_LIMIT = 3;

function addFlag(flags, flag) {
  if (!flags.includes(flag)) flags.push(flag);
}

function isUnknown(value) {
  const normalized = String(value ?? '').trim().toLowerCase();
  return normalized === '' || normalized === 'unknown' || normalized === 'not listed';
}

function usable(value) {
  const normalized = String(value ?? '').trim().toLowerCase();
  return normalized !== '' && !['none', 'none found', 'unknown', 'n/a', 'not listed', 'not found', 'not available'].includes(normalized);
}

// Flags are split on commas and semicolons downstream, so firm names must not contain either.
function cleanFirm(value) {
  return String(value || '').replace(/[;,]/g, ' ').replace(/\s+/g, ' ').trim();
}

function normalizeDocket(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/^\d+\s*:\s*/, '')
    .replace(/\s+/g, '');
}

function hasInsufficientProfile(item) {
  const confidence = String(item.confidence || '').trim().toLowerCase();
  const sizeEvidence = String(item.sizeEvidence || '').trim().toLowerCase();
  const notes = String(item.notes || '').trim().toLowerCase();
  const profileFailed = confidence === 'research failed' || sizeEvidence === 'research failed' || notes === 'research failed';
  return profileFailed || isUnknown(item.hqState) || isUnknown(item.sizeBand);
}

const researchByDocket = new Map();
for (const research of researchResults) {
  const key = normalizeDocket(research && research.docketNumber);
  if (key) researchByDocket.set(key, research);
}

const enrichedResearch = researchCases.map(base => {
  const docket = normalizeDocket(base.docketNumber || base.caseNumber);
  const research = researchByDocket.get(docket) || {};
  const hasMatchingResearch = Boolean(researchByDocket.get(docket));
  const flags = String(base.Flags || '')
    .replace(/;\s*/g, ', ')
    .split(/\s*,\s*/)
    .map(value => value.trim())
    .filter(Boolean);

  const score = Number(base.Score || 0);
  let disqualified = String(base.Flags || '').startsWith('Disqualified:');
  const entityProfileFailed =
    flags.includes('Research failed and review manually') ||
    String(base.confidence || '').toLowerCase() === 'research failed';

  const matters = Array.isArray(research.matters) ? research.matters : [];
  // The new docket often appears as a matter with no counsel yet, so it only counts when a firm is named.
  const sameDocketMatter = matters.some(
    matter => normalizeDocket(matter.caseNumber) === docket && Boolean(docket) && usable(matter.counselFirm)
  );

  const researchFailed =
    !hasMatchingResearch ||
    String(research.summary || '').toLowerCase() === 'research failed' ||
    // A "not researched" reply for a selected candidate means the search never ran.
    /^not researched/i.test(String(research.summary || '').trim()) ||
    String(research.confidence || '').toLowerCase() === 'research failed';

  // Firms tied to the employer or a related entity in any matter found.
  const firms = [];
  for (const matter of matters) {
    const match = String(matter.entityMatch || '').toLowerCase();
    if ((match === 'exact' || match === 'related') && usable(matter.counselFirm)) {
      const firm = cleanFirm(matter.counselFirm);
      if (firm && !firms.includes(firm)) firms.push(firm);
    }
  }

  const counselObserved = String(research.counselObserved || '').toLowerCase() === 'yes';
  const nationalFirm = String(research.retainedNationalFirmSignal || '').toLowerCase() === 'yes';

  // Counsel found for the employer, a related entity, or on this docket means it is already represented.
  if (counselObserved || nationalFirm || firms.length > 0 || sameDocketMatter) {
    disqualified = true;
    const detail = firms.length ? firms.slice(0, 3).join(' / ') : 'see Counsel Summary';
    flags.unshift(`Disqualified: Already represented (${detail})`);
    if (nationalFirm) addFlag(flags, 'Retained national firm');
  }

  if (String(research.confidence || '').toLowerCase() === 'low') {
    addFlag(flags, 'Litigation research low confidence');
  }

  if (researchFailed) addFlag(flags, 'Research failed');

  const insufficientProfile = hasInsufficientProfile(base);
  if (insufficientProfile) addFlag(flags, 'Insufficient profile');

  const why = disqualified
    ? `Not recommended: ${flags[0].replace(/^Disqualified:\s*/, '')}.`
    : base.whyRecommended;

  return {
    ...base,
    litigationMatters: research.matters || [],
    counselObserved: research.counselObserved || 'unknown',
    counselSummary: research.counselSummary || '',
    retainedNationalFirmSignal: research.retainedNationalFirmSignal || 'unknown',
    litigationSummary: research.summary || '',
    litigationCategoriesSearched: research.categoriesSearched || [],
    litigationConfidence: research.confidence || 'unknown',
    Score: score,
    Flags: flags.join(', '),
    whyRecommended: why,
    _disqualified: disqualified,
    _profileFailed: entityProfileFailed,
    _insufficientProfile: insufficientProfile,
    // If the litigation search failed we cannot say the employer is unrepresented.
    _researchFailed: researchFailed
  };
});

const enrichedByDocket = new Map(
  enrichedResearch.map(item => [
    normalizeDocket(item.docketNumber || item.caseNumber),
    item
  ])
);

const finalCases = allCases.map(item => {
  const key = normalizeDocket(item.docketNumber || item.caseNumber);
  return enrichedByDocket.get(key) || item;
});

for (const item of finalCases) {
  item.recommendOutreach = 'No';
  if (hasInsufficientProfile(item) || item._insufficientProfile === true) {
    item.researchCandidate = false;
    const flags = String(item.Flags || '')
      .replace(/;\s*/g, ', ')
      .split(/\s*,\s*/)
      .map(value => value.trim())
      .filter(Boolean);
    addFlag(flags, 'Insufficient profile');
    item.Flags = flags.join(', ');
    const why = String(item.whyRecommended || '').trim();
    item.whyRecommended = why.includes('Insufficient profile')
      ? why
      : `${why}${why ? ' ' : ''}Insufficient profile.`;
  }
}

const eligible = finalCases
  .filter(
    item =>
      item.researchCandidate === true &&
      !hasInsufficientProfile(item) &&
      !String(item.Flags || '').startsWith('Disqualified:') &&
      !item._disqualified &&
      !item._profileFailed &&
      !item._insufficientProfile &&
      !item._researchFailed &&
      Number(item.Score || 0) >= MIN_SCORE
  )
  .sort(
    (a, b) =>
      Number(b.Score || 0) - Number(a.Score || 0) ||
      String(a.caseInitiatedDate || '').localeCompare(
        String(b.caseInitiatedDate || '')
      )
  );

for (const item of eligible.slice(0, OUTREACH_LIMIT)) {
  item.recommendOutreach = 'Yes';
}

for (const item of finalCases) {
  delete item._disqualified;
  delete item._profileFailed;
  delete item._insufficientProfile;
  delete item._researchFailed;
}

return { cases: finalCases };
