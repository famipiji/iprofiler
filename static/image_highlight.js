/*
 * Field-value highlighting on image documents (jpg/png), used by
 * templates/index.html. Images have no PDF.js text layer, so matches are found
 * in the OCR text blocks the server returns for the document
 * ([{page, text, bbox: {x1, y1, x2, y2}}], 0-1 fractions, top-left origin)
 * and drawn over the <img>.
 *
 * The boxes are in the frame the browser displays: the server OCRs an
 * uploaded photo upright, with its EXIF orientation applied
 * (qwen_service.open_upright), and the <img> is shown the same way
 * (image-orientation: from-image), so they line up without any rotation here.
 *
 * Loaded by the page as window.ImageHighlight; also a CommonJS module so
 * test_image_highlight.py can run it under node.
 */
(function (root) {
  'use strict';

  /*
   * Rects (stored frame, 0-1) to mark for `value` in `blocks`, using
   * `matcher(text, value) -> [{index, length}]` (the page's exact-then-date
   * matcher). A match inside one block is narrowed horizontally to the matched
   * characters (proportional to their position in the line); when no single
   * block matches, consecutive blocks (2..maxJoin) are joined with a space and
   * a match spanning them marks each block it covers.
   */
  function findBlockMatches(blocks, value, matcher, maxJoin) {
    maxJoin = maxJoin || 4;
    var rects = [];
    var list = (blocks || []).filter(function (b) { return b && b.text && b.bbox; });

    list.forEach(function (b) {
      var len = b.text.length;
      matcher(b.text, value).forEach(function (m) {
        var w = b.bbox.x2 - b.bbox.x1;
        rects.push({
          x1: b.bbox.x1 + w * (m.index / len),
          y1: b.bbox.y1,
          x2: b.bbox.x1 + w * ((m.index + m.length) / len),
          y2: b.bbox.y2,
        });
      });
    });
    if (rects.length) return rects;

    for (var k = 2; k <= maxJoin; k++) {
      for (var i = 0; i + k <= list.length; i++) {
        var group = list.slice(i, i + k);
        if (group.some(function (b) { return b.page !== group[0].page; })) continue;
        var starts = [], joined = '';
        group.forEach(function (b, gi) {
          if (gi) joined += ' ';
          starts.push(joined.length);
          joined += b.text;
        });
        matcher(joined, value).forEach(function (m) {
          var end = m.index + m.length;
          // Only matches that really span this whole window (start in its
          // first block, end in its last) - shorter ones belong to a smaller k.
          if (m.index >= starts[1] || end <= starts[k - 1]) return;
          group.forEach(function (b) { rects.push({ x1: b.bbox.x1, y1: b.bbox.y1, x2: b.bbox.x2, y2: b.bbox.y2 }); });
        });
      }
      if (rects.length) return rects;
    }
    return rects;
  }

  /* A rect (0-1 fractions) in CSS px for an image shown at width x height. */
  function toPixels(r, width, height) {
    return { left: r.x1 * width, top: r.y1 * height, width: (r.x2 - r.x1) * width, height: (r.y2 - r.y1) * height };
  }

  var api = {
    findBlockMatches: findBlockMatches,
    toPixels: toPixels,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.ImageHighlight = api;
})(typeof window !== 'undefined' ? window : globalThis);
