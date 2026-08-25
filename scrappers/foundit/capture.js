/*
 * foundit.in capture snippet — run in the DevTools console on any
 * https://www.foundit.in/ page (e.g. /search/healthcare-jobs).
 *
 * Walks the healthcare keyword SEO pages, extracts the jobSearchAPIData
 * cards from each page's RSC flight stream (resolving description text-chunk
 * refs), and POSTs the result to the local receiver
 * (python3 receiver.py captures/<DD-MM-YYYY>.json 8765).
 *
 * Akamai rate-limits bursts (~25 fast loads -> temporary domain-wide 403),
 * so this paces itself: 4-7 s jitter per page, a 45 s breather every 12
 * pages, and it POSTs partial results as it goes. On a 403 it backs off
 * 90 s before retrying; three failures end the keyword, not the run.
 *
 * Progress: window.__cap.{status,progress,keywords,errors}
 * Stop early: window.__cap.abort = true   (partial is still POSTed)
 * Resume: window.__capStart = {healthcare: 13}  // skip pages < 13
 */
window.__cap = {status:'starting', progress:'', keywords:{}, jobs:{},
                errors:[], abort:false};
(async () => {
  const cap = window.__cap;
  const START = window.__capStart || {};
  const KEYWORDS = ['healthcare','medical','doctor','nurse',
    'medical-representative','physiotherapist','pharmacist','lab-technician',
    'radiographer','hospital','paramedical','dentist','medical-coding',
    'nursing',
    // 2026-08-24 role-family widening: fetch wide, filter at ingest.
    // Niche terms end after a page or two, so the added run time is small.
    'clinical-research','clinical-trials','clinical-data-management',
    'pharmacovigilance','drug-safety','regulatory-affairs','medical-writing',
    'medical-affairs','market-access','public-health','epidemiology',
    // 2026-08-25 Public Health widening: cover all ten PH sub-categories,
    // mirroring the shine_roles list. Niche terms end after a page or two,
    // so the added run time stays small.
    'epidemiologist','disease-surveillance',
    'public-health-program',
    'monitoring-and-evaluation',
    'community-health-officer','asha',
    'health-educator','health-promotion',
    'tuberculosis','hiv','malaria','immunization','vaccination',
    'public-health-nutrition','nutritionist',
    'infection-control',
    'health-informatics','hmis',
    'public-health-research'];
  const MAX_PAGES = 30;          // per keyword; scraper logs every cap hit
  const LIMIT = 20;              // cards per page (site constant)
  const DELAY_MS = 4000, JITTER_MS = 3000;
  const BREATHER_EVERY = 12, BREATHER_MS = 45000;
  const RECEIVER = 'http://127.0.0.1:8765/';
  const enc = new TextEncoder();
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let pagesFetched = 0;

  const getStream = (html) => {
    const re = /self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)/g;
    let m, chunks = [];
    while ((m = re.exec(html))) { try { chunks.push(JSON.parse(m[1])); } catch (e) {} }
    return chunks.join('');
  };

  // T-chunks ("2b:T<hexlen>,<utf8 bytes>") are length-prefixed in BYTES and
  // not newline-separated: resolve sequentially, binary-searching the JS
  // char count whose UTF-8 encoding matches the byte length.
  const tChunkMap = (s) => {
    const map = {}; const rx = /([0-9a-f]{1,4}):T([0-9a-f]+),/g; let m, pos = 0;
    while ((m = rx.exec(s))) {
      if (m.index < pos) continue;          // inside a previous chunk's text
      const start = m.index + m[0].length;
      const byteLen = parseInt(m[2], 16);
      let lo = 0, hi = Math.min(s.length - start, byteLen);
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (enc.encode(s.slice(start, start + mid)).length <= byteLen) lo = mid;
        else hi = mid - 1;
      }
      map[m[1]] = s.slice(start, start + lo);
      pos = start + lo; rx.lastIndex = pos;
    }
    return map;
  };

  const extract = (html) => {
    const s = getStream(html);
    const key = '"jobSearchAPIData":';
    const at = s.indexOf(key);
    if (at < 0) return null;
    let depth = 0, i = at + key.length, inStr = false, esc = false;
    const start = i;
    for (;; i++) {
      const c = s[i];
      if (c === undefined) return null;
      if (esc) { esc = false; continue; }
      if (c === '\\') { esc = true; continue; }
      if (inStr) { if (c === '"') inStr = false; continue; }
      if (c === '"') { inStr = true; continue; }
      if (c === '{' || c === '[') depth++;
      else if (c === '}' || c === ']') { depth--; if (depth === 0) { i++; break; } }
    }
    let obj; try { obj = JSON.parse(s.slice(start, i)); } catch (e) { return null; }
    const tmap = tChunkMap(s);
    const jobs = (obj.data || []).map(j => {
      let desc = j.description;
      if (typeof desc === 'string' && desc.startsWith('$')) desc = tmap[desc.slice(1)] ?? '';
      if (desc === '$undefined' || desc == null) desc = '';
      return {
        jobId: j.jobId, title: j.title,
        companyName: (j.company && j.company.name) || j.companyName || '',
        hideCompanyName: !!j.hideCompanyName,
        locations: (j.locations || []).map(l => l.city).filter(Boolean),
        minExpYears: j.minimumExperience && j.minimumExperience.years,
        maxExpYears: j.maximumExperience && j.maximumExperience.years,
        minSalary: j.minimumSalary, maxSalary: j.maximumSalary,
        hideSalary: !!j.hideSalary, currencyCode: j.currencyCode,
        postedAt: j.postedAt, createdAt: j.createdAt, updatedAt: j.updatedAt,
        industries: j.industries || [], functions: j.functions || [],
        jobTypes: j.jobTypes || [], employmentTypes: j.employmentTypes || [],
        skills: (j.skills || []).map(x => x.text || x).filter(x => typeof x === 'string'),
        jdUrl: j.jdUrl || '', redirectUrl: j.redirectUrl || '',
        quickJob: !!j.quickJob, totalApplicants: j.totalApplicants,
        description: desc,
      };
    });
    return { jobs, total: obj.meta && obj.meta.paging && obj.meta.paging.total };
  };

  const post = async (final) => {
    const payload = {
      captured_at: new Date().toISOString(), site: 'foundit.in',
      mode: 'seo-search-pages-rsc', partial: !final,
      keywords: cap.keywords, errors: cap.errors,
      jobs: Object.values(cap.jobs),
    };
    try {
      const r = await fetch(RECEIVER, {method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify(payload)});
      return r.ok;
    } catch (e) { cap.errors.push('POST: ' + e); return false; }
  };

  cap.status = 'running';
  for (const kw of KEYWORDS) {
    if (cap.abort) break;
    const kstat = cap.keywords[kw] =
      {total: null, pages: 0, jobs: 0, capped: false, errors: 0};
    let pages = Math.max(1, START[kw] || 1);
    let lastPage = pages;                      // grows once total is known
    for (let p = pages; p <= lastPage; p++) {
      if (cap.abort) break;
      const url = p === 1 ? `/search/${kw}-jobs` : `/search/${kw}-jobs-${p}`;
      cap.progress = `${kw} page ${p}/${lastPage}`;
      let got = null;
      for (let attempt = 1; attempt <= 3 && !got && !cap.abort; attempt++) {
        try {
          const r = await fetch(url, {credentials: 'include'});
          if (r.status === 200) got = await r.text();
          else {
            cap.errors.push(`${url} HTTP ${r.status}`);
            await sleep(r.status === 403 ? 90000 : attempt * 8000);
          }
        } catch (e) { cap.errors.push(`${url} ${e}`); await sleep(attempt * 8000); }
      }
      if (!got) { kstat.errors++; break; }
      const res = extract(got);
      if (!res || !res.jobs.length) break;     // past the last page
      if (kstat.total == null) {
        kstat.total = res.total ?? null;
        const want = Math.ceil((res.total || LIMIT) / LIMIT);
        lastPage = Math.min(want, MAX_PAGES);
        kstat.capped = want > MAX_PAGES;
      }
      for (const j of res.jobs) {
        if (!j.jobId) continue;
        if (!cap.jobs[j.jobId]) { cap.jobs[j.jobId] = j; j.foundVia = [kw]; }
        else if (!cap.jobs[j.jobId].foundVia.includes(kw))
          cap.jobs[j.jobId].foundVia.push(kw);
      }
      kstat.pages = p; kstat.jobs += res.jobs.length;
      pagesFetched++;
      if (pagesFetched % BREATHER_EVERY === 0) {
        cap.progress += ' (breather)';
        await post(false);                     // save partial along the way
        await sleep(BREATHER_MS);
      } else {
        await sleep(DELAY_MS + Math.random() * JITTER_MS);
      }
    }
  }
  cap.status = 'posting';
  cap.status = (await post(true)) ? 'done' : 'post_failed';
})();
'capture started'
