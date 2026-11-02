/*
 * Date-aware matching for field-click highlighting (templates/index.html).
 *
 * The model normalizes dates (field "2026-09-23") while the document prints
 * them its own way ("9/23/2026", "23.09.26", "23 Sept 2026", "23 Ogos 2026"),
 * so an exact substring search never finds them. For a value that looks like
 * a date this builds its common printed variants and finds the first one that
 * occurs in a page's text, tolerating extra spaces around separators
 * ("9 / 23 / 2026"). Non-date values return null and callers keep their exact
 * substring match.
 *
 * Loaded by the page as window.DateHighlight; also a CommonJS module so
 * test_date_highlight.py can run it under node.
 */
(function (root) {
  'use strict';

  var EN_FULL  = ['january', 'february', 'march', 'april', 'may', 'june', 'july',
                  'august', 'september', 'october', 'november', 'december'];
  var EN_SHORT = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
  var MS_FULL  = ['januari', 'februari', 'mac', 'april', 'mei', 'jun', 'julai',
                  'ogos', 'september', 'oktober', 'november', 'disember'];
  var MS_SHORT = ['jan', 'feb', 'mac', 'apr', 'mei', 'jun', 'jul', 'ogo', 'sep', 'okt', 'nov', 'dis'];

  // Every spelling a month name can take in a value -> month number (1-12).
  var MONTH_LOOKUP = {};
  [EN_FULL, EN_SHORT, MS_FULL, MS_SHORT].forEach(function (list) {
    list.forEach(function (name, i) { MONTH_LOOKUP[name] = i + 1; });
  });
  MONTH_LOOKUP.sept = 9;

  function pad2(n) { return n < 10 ? '0' + n : String(n); }

  function fullYear(y) {
    y = parseInt(y, 10);
    if (y >= 100) return y;
    return y < 70 ? 2000 + y : 1900 + y;
  }

  function validDate(y, m, d) {
    if (m < 1 || m > 12 || d < 1 || y < 1000) return false;
    var days = [31, (y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0)) ? 29 : 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    return d <= days[m - 1];
  }

  /*
   * The date(s) a value may mean, as [{y, m, d}], most likely first; [] if it
   * doesn't look like a date. A numeric D/M vs M/D value that could be either
   * (05/06/2026) yields both readings, day-first first.
   */
  function parseDates(value) {
    var s = String(value == null ? '' : value).trim().toLowerCase().replace(/\s+/g, ' ');
    var out = [];
    function add(y, m, d) {
      if (validDate(y, m, d) && !out.some(function (o) { return o.y === y && o.m === m && o.d === d; })) {
        out.push({ y: y, m: m, d: d });
      }
    }
    var mt;
    // ISO YYYY-MM-DD (also with / or . and an optional time part)
    if ((mt = s.match(/^(\d{4})\s*[-\/.]\s*(\d{1,2})\s*[-\/.]\s*(\d{1,2})(?:[t ]\d{1,2}:\d{2}.*)?$/))) {
      add(+mt[1], +mt[2], +mt[3]);
      return out;
    }
    // D/M/Y or M/D/Y with / - . separators, 2- or 4-digit year
    if ((mt = s.match(/^(\d{1,2})\s*([-\/.])\s*(\d{1,2})\s*\2\s*(\d{2}|\d{4})$/))) {
      var a = +mt[1], b = +mt[3], y = fullYear(mt[4]);
      add(y, b, a);   // day-first
      add(y, a, b);   // month-first
      return out;
    }
    // D Mon YYYY  (23 Sep 2026, 23-Sep-2026, 23rd September, 2026, 23 Ogos 2026)
    if ((mt = s.match(/^(\d{1,2})(?:st|nd|rd|th)?[\s\-\/.,]*([a-z]+)\.?[\s\-\/.,]*(\d{2}|\d{4})$/))) {
      if (MONTH_LOOKUP[mt[2]]) add(fullYear(mt[3]), MONTH_LOOKUP[mt[2]], +mt[1]);
      return out;
    }
    // Mon D, YYYY  (Sep 23, 2026 / September 23 2026)
    if ((mt = s.match(/^([a-z]+)\.?[\s\-\/.]*(\d{1,2})(?:st|nd|rd|th)?[\s,]*(\d{2}|\d{4})$/))) {
      if (MONTH_LOOKUP[mt[1]]) add(fullYear(mt[3]), MONTH_LOOKUP[mt[1]], +mt[2]);
      return out;
    }
    return out;
  }

  function uniquePush(list, seen, v) {
    if (!seen[v]) { seen[v] = true; list.push(v); }
  }

  /*
   * Printed variants of a date value (lowercase), in the order they are tried:
   * the value itself, ISO forms, numeric day-first / month-first forms (with
   * and without leading zeros, / - . separators, 4- then 2-digit year), then
   * English and Malay month-name forms. [] for a non-date value.
   */
  function dateVariants(value) {
    var dates = parseDates(value);
    if (!dates.length) return [];
    var list = [], seen = {};
    uniquePush(list, seen, String(value).trim().toLowerCase().replace(/\s+/g, ' '));
    var seps = ['/', '-', '.'];

    dates.forEach(function (dt) {
      var Y = String(dt.y), YY = Y.slice(-2);
      var days = [pad2(dt.d), String(dt.d)], months = [pad2(dt.m), String(dt.m)];
      seps.forEach(function (sep) { uniquePush(list, seen, Y + sep + pad2(dt.m) + sep + pad2(dt.d)); });
      [Y, YY].forEach(function (yr) {
        seps.forEach(function (sep) {
          days.forEach(function (d) {
            months.forEach(function (m) {
              uniquePush(list, seen, d + sep + m + sep + yr);   // day-first
              uniquePush(list, seen, m + sep + d + sep + yr);   // month-first
            });
          });
        });
      });
    });

    dates.forEach(function (dt) {
      var Y = String(dt.y), YY = Y.slice(-2);
      var names = [EN_FULL[dt.m - 1], EN_SHORT[dt.m - 1], MS_FULL[dt.m - 1], MS_SHORT[dt.m - 1]];
      if (dt.m === 9) names.push('sept');
      var days = [String(dt.d), pad2(dt.d)];
      names.forEach(function (mon) {
        [Y, YY].forEach(function (yr) {
          days.forEach(function (d) {
            uniquePush(list, seen, d + ' ' + mon + ' ' + yr);
            uniquePush(list, seen, d + '-' + mon + '-' + yr);
            uniquePush(list, seen, mon + ' ' + d + ', ' + yr);
            uniquePush(list, seen, mon + ' ' + d + ' ' + yr);
          });
        });
      });
    });
    return list;
  }

  function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  /*
   * Regex source for one variant: any amount of whitespace (or none - PDF.js
   * text-layer spans are joined without spaces) around separators and between
   * tokens, an optional "." after a month name, an optional ordinal after a
   * day, and no digit/letter directly before or after (so "1/2/2026" doesn't
   * match inside "11/2/20261").
   */
  function variantPattern(variant) {
    var tokens = variant.match(/\d+|[a-z]+|[^\s\da-z]|\s+/g) || [];
    var hasMonthName = /[a-z]/.test(variant);
    var src = '';
    tokens.forEach(function (t) {
      if (/^\s+$/.test(t)) { src += '\\s*'; return; }
      if (/^\d+$/.test(t)) {
        // The day in a month-name form may carry an ordinal: "23rd September", "Sep 23rd, 2026"
        src += t + (hasMonthName && t.length <= 2 ? '(?:st|nd|rd|th)?' : '');
        return;
      }
      if (/^[a-z]+$/.test(t)) { src += escapeRe(t) + (MONTH_LOOKUP[t] && t.length <= 4 ? '\\.?' : ''); return; }
      src += '\\s*' + escapeRe(t) + '\\s*';   // separator: / - . ,
    });
    var first = variant.charAt(0), last = variant.charAt(variant.length - 1);
    var pre  = /\d/.test(first) ? '(?<![0-9])' : '(?<![a-z])';
    var post = /\d/.test(last)  ? '(?![0-9])'  : '(?![a-z])';
    return pre + src + post;
  }

  /*
   * For a date value: the first variant (in dateVariants() order) that occurs
   * in `text`, with every occurrence of it as {index, length} into `text`.
   * null if `value` isn't a date or no variant occurs.
   */
  function findDateMatches(text, value) {
    var variants = dateVariants(value);
    if (!variants.length || !text) return null;
    for (var i = 0; i < variants.length; i++) {
      var re = new RegExp(variantPattern(variants[i]), 'gi');
      var matches = [], m;
      while ((m = re.exec(text)) !== null) {
        if (!m[0].length) { re.lastIndex++; continue; }
        matches.push({ index: m.index, length: m[0].length });
      }
      if (matches.length) return { variant: variants[i], matches: matches };
    }
    return null;
  }

  var api = {
    parseDates: parseDates,
    dateVariants: dateVariants,
    variantPattern: variantPattern,
    findDateMatches: findDateMatches,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.DateHighlight = api;
})(typeof window !== 'undefined' ? window : globalThis);
