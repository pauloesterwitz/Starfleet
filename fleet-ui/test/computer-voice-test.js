/* Self-check of the pure parts of vendor/computer-voice.js (markdown -> speech text, sentence splitting, streaming chunker).
   node fleet-ui/test/computer-voice-test.js */
const assert = require("assert");
const CV = require(process.env.CV_JS || require("path").join(__dirname, "..", "vendor", "computer-voice.js"));
const { clean, sentences, Splitter, cut } = CV;

// markdown -> speech
assert.strictEqual(clean("Hello **world**, this is `code` and a [link](https://example.com/x)."), "Hello world, this is code and a link.");
assert.strictEqual(clean("Before.\n```js\nconst x = 1;\n```\nAfter."), "Before. After.", "fenced code is skipped");
assert.strictEqual(clean("Before.\n```js\nconst x = 1;\nstill open"), "Before.", "an open fence swallows the rest");
assert.strictEqual(clean("## Summary\n- first item\n- second item\n1. third"), "Summary. first item. second item. third.", "headings and list items become sentences");
assert.strictEqual(clean("| a | b |\n|---|---|\n| 1 | 2 |\nDone."), "Done.", "tables are not read");
assert.strictEqual(clean("See https://example.com/very/long/url now."), "See now.", "bare URLs are dropped");
assert.strictEqual(clean("Use wiki_search and *emphasis* here 🙂"), "Use wiki search and emphasis here.", "underscores, emphasis and emoji");
assert.strictEqual(clean("---\n***\nText"), "Text.");
assert.strictEqual(clean("![chart](data:image/png;base64,AAAA) Caption here"), "Caption here.");
assert.strictEqual(clean(""), "");

// sentences
assert.deepStrictEqual(sentences("Working. The result is ready. Three items."), ["Working.", "The result is ready.", "Three items."]);

// long sentence cut: every part within the request limit, nothing lost
const long = Array.from({ length: 60 }, (_, i) => "word" + i).join(" ") + ", and then " + "x ".repeat(100);
const parts = cut(long);
assert(parts.length > 1 && parts.every(p => p.length <= 260), "parts stay under 260 characters");
assert.strictEqual(parts.join(" ").replace(/\s+/g, " "), long.trim().replace(/\s+/g, " "));

// streaming: feed the reply token by token; chunks come out as sentences complete; end() flushes; nothing is lost or repeated
const reply = "Working. The computation is complete.\n\n## Results\n\n- Three results match your query, and the most relevant one is listed first.\n- The second is older.\n\n```py\nprint('never read')\n```\n\nDie Berechnung ist abgeschlossen. Das war es schon.";
const sp = new Splitter(), got = [];
let first = null;
for (let i = 1; i <= reply.length; i += 3) {
  const out = sp.feed(reply.slice(0, i));
  if (out.length && first === null) first = i;
  got.push(...out);
}
assert(first !== null && first < reply.length / 2, "the first chunk is ready long before the reply ends");
got.push(...sp.end(reply));
const spoken = got.join(" ").replace(/\s+/g, " ");
assert.strictEqual(spoken, sentences(clean(reply)).join(" ").replace(/\s+/g, " "), "streamed chunks equal the whole reply, once");
assert(!/never read|print/.test(spoken), "the code block never reaches the voice");
assert(got.slice(1).every(c => c.length >= 60 || c === got[got.length - 1]), "after the first piece, chunks are 60+ characters (the last may be shorter)");
assert(got.every(c => c.length <= 330), "no chunk beats the server's 600 limit with room to spare");

// a reply that is only code: nothing to say
assert.deepStrictEqual(new Splitter().end("```\nx\n```"), []);
console.log("ok - computer-voice.js: markdown cleaning, sentences, long-sentence cut, streaming chunker (" + got.length + " chunks)");
