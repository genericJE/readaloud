//! Source code as a syntax tree: the tree-sitter runtime, with each
//! language's grammar loaded at run time rather than compiled in.
//!
//! readaloud ships no grammar.  A grammar is a `TSLanguage` that the
//! grammar's own code hands out: the `tree-sitter-<language>` wheel on PyPI
//! gives it as a `tree_sitter.Language` capsule, and a `parser/<language>.so`
//! as Neovim builds them exports a function `tree_sitter_<language>` that
//! returns it.  Either way the runtime checks the grammar's ABI version
//! before it parses with it, as Neovim does.

use std::collections::HashMap;
use std::ffi::c_void;
use std::path::Path;
use std::sync::{Arc, Mutex};

use tree_sitter::ffi::TSLanguage;
use tree_sitter::{Language, Node, Parser, Tree, LANGUAGE_VERSION, MIN_COMPATIBLE_LANGUAGE_VERSION};
use tree_sitter_highlight::{HighlightConfiguration, HighlightEvent, Highlighter};

use crate::shapes::{shared, swapped, Found, Piece};

/// A language's grammar, ready to parse with.
pub struct Grammar {
    language: Language,
    /// the shared library the grammar came from, kept open as long as the
    /// grammar is used
    _library: Option<libloading::Library>,
}

impl Grammar {
    /// From a `TSLanguage` pointer, such as the one a `tree_sitter.Language`
    /// capsule holds.
    ///
    /// # Safety
    /// `ptr` must point at a `TSLanguage` that stays valid as long as the
    /// grammar is used; a wheel's is static data of a module Python never
    /// unloads.
    pub unsafe fn from_raw(ptr: *const c_void) -> Result<Self, String> {
        if ptr.is_null() {
            return Err("the grammar is a null pointer".into());
        }
        // Language is a transparent wrapper round the pointer, and dropping
        // one does nothing for a grammar that is not WebAssembly
        let language: Language = unsafe { std::mem::transmute(ptr as *const TSLanguage) };
        check(&language)?;
        Ok(Grammar { language, _library: None })
    }

    /// From the shared library at `path`, which exports
    /// `tree_sitter_<name>`.
    pub fn load(path: &Path, name: &str) -> Result<Self, String> {
        let shown = path.display();
        // SAFETY: loading a library runs its initialisers; a grammar's has
        // none, and this is the same step Neovim takes for the same file
        let library = unsafe { libloading::Library::new(path) }
            .map_err(|e| format!("cannot load {shown}: {e}"))?;
        let symbol = format!("tree_sitter_{name}");
        let language = unsafe {
            let entry: libloading::Symbol<unsafe extern "C" fn() -> *const TSLanguage> = library
                .get(symbol.as_bytes())
                .map_err(|_| format!("{shown} holds no grammar called {name:?} (it has no {symbol})"))?;
            let ptr = entry();
            if ptr.is_null() {
                return Err(format!("{shown}: {symbol} gave no grammar"));
            }
            std::mem::transmute::<*const TSLanguage, Language>(ptr)
        };
        check(&language)?;
        Ok(Grammar { language, _library: Some(library) })
    }

    /// The grammar's ABI version.
    pub fn abi_version(&self) -> usize {
        self.language.abi_version()
    }

    /// The kinds of node the grammar makes, named ones first
    /// (`function_definition`), then the anonymous ones (`def`, `(`).
    pub fn node_kinds(&self) -> Vec<String> {
        let ids = 0..self.language.node_kind_count() as u16;
        let kinds: Vec<(bool, &str)> = ids
            .filter(|&id| self.language.node_kind_is_visible(id))
            .filter_map(|id| Some((self.language.node_kind_is_named(id), self.language.node_kind_for_id(id)?)))
            .collect();
        let mut out: Vec<String> = Vec::new();
        for named in [true, false] {
            for &(is_named, kind) in &kinds {
                if is_named == named && !out.iter().any(|k| k == kind) {
                    out.push(kind.to_string());
                }
            }
        }
        out
    }

    /// The names of the fields the grammar's nodes have (`name`, `parameters`).
    pub fn field_names(&self) -> Vec<String> {
        (1..=self.language.field_count() as u16)
            .filter_map(|id| self.language.field_name_for_id(id))
            .map(str::to_string)
            .collect()
    }

    /// The syntax tree of `text`.
    pub fn parse(&self, text: &str) -> Option<Tree> {
        let mut parser = Parser::new();
        parser.set_language(&self.language).ok()?;
        parser.parse(text, None)
    }

    /// Every stretch of `text` the highlights `query` (the grammar's
    /// `highlights.scm`) colours: `(start, end, name)` in characters, `name`
    /// an index into `names`, the capture names recognised.  A capture takes
    /// the name it starts with (`function.method` is `function`); one no
    /// name fits is not coloured, and the innermost capture wins.  Err when
    /// the query cannot be used with this grammar.
    pub fn highlight(&self, text: &str, query: &str, names: &[String]) -> Result<Vec<(usize, usize, usize)>, String> {
        let mut config = HighlightConfiguration::new(self.language.clone(), "readaloud", query, "", "")
            .map_err(|e| e.to_string())?;
        config.configure(names);
        let mut highlighter = Highlighter::new();
        let events = highlighter
            .highlight(&config, text.as_bytes(), None, |_| None)
            .map_err(|e| e.to_string())?;
        // character offset of each byte that starts a character, plus the end
        let mut char_at = vec![0; text.len() + 1];
        for (c, (b, ch)) in text.char_indices().enumerate() {
            for x in b..b + ch.len_utf8() {
                char_at[x] = c;
            }
        }
        char_at[text.len()] = text.chars().count();
        let mut stack = Vec::new();
        let mut out = Vec::new();
        for event in events {
            match event.map_err(|e| e.to_string())? {
                HighlightEvent::HighlightStart(h) => stack.push(h.0),
                HighlightEvent::HighlightEnd => {
                    stack.pop();
                }
                HighlightEvent::Source { start, end } => {
                    if let (Some(&name), true) = (stack.last(), start < end) {
                        out.push((char_at[start], char_at[end], name));
                    }
                }
            }
        }
        Ok(out)
    }
}

/// Err unless the runtime can use a grammar of `language`'s ABI version.
fn check(language: &Language) -> Result<(), String> {
    let version = language.abi_version();
    if (MIN_COMPATIBLE_LANGUAGE_VERSION..=LANGUAGE_VERSION).contains(&version) {
        return Ok(());
    }
    Err(format!(
        "the grammar's ABI version is {version}; this readaloud reads \
         {MIN_COMPATIBLE_LANGUAGE_VERSION} to {LANGUAGE_VERSION} (install a \
         tree-sitter-<language> of about the same age)"
    ))
}

/// `tree` one node per line, indented by depth: the field name when the node
/// is one, its kind, its byte range, `!` for an error or a missing node, and
/// a leaf's text.  For looking at what a grammar makes of some text.
pub fn outline(tree: &Tree, text: &str) -> String {
    let mut out = String::new();
    let mut cursor = tree.walk();
    let mut depth = 0usize;
    loop {
        let node = cursor.node();
        out.push_str(&"  ".repeat(depth));
        if let Some(field) = cursor.field_name() {
            out.push_str(field);
            out.push_str(": ");
        }
        out.push_str(node.kind());
        out.push_str(&format!(" [{}..{}]", node.start_byte(), node.end_byte()));
        if node.is_error() || node.is_missing() {
            out.push_str(" !");
        }
        if node.child_count() == 0 {
            out.push_str(&format!("  {:?}", text.get(node.byte_range()).unwrap_or("")));
        }
        out.push('\n');
        if cursor.goto_first_child() {
            depth += 1;
            continue;
        }
        loop {
            if cursor.goto_next_sibling() {
                break;
            }
            if !cursor.goto_parent() {
                return out;
            }
            depth -= 1;
        }
    }
}

/// How deep a tree is followed before a node is said as written.
const MAX_DEPTH: usize = 256;

/// The `[syntax]` rules: a node kind, `ancestor/kind` for such a node
/// anywhere under a node of the first kind, or `kind.field` for whatever
/// node is that field of such a node, and how to say it.  A later rule for
/// the same key replaces an earlier one.
pub struct Rules {
    by_key: HashMap<String, String>,
}

impl Rules {
    pub fn new(rules: &[(String, String)]) -> Self {
        let by_key = rules.iter().map(|(k, w)| (k.clone(), crate::text::collapse(w))).collect();
        Rules { by_key }
    }

    /// The wording for `node`: `kind.field` for the field it is of its
    /// parent, else `ancestor/kind` for its nearest ancestor that has one,
    /// else its own kind's.  Empty says the node as written.
    fn wording(&self, node: Node) -> Option<&str> {
        if let Some(parent) = node.parent() {
            let mut cursor = parent.walk();
            let field = parent
                .children(&mut cursor)
                .position(|c| c == node)
                .and_then(|i| parent.field_name_for_child(i as u32));
            if let Some(field) = field {
                if let Some(w) = self.by_key.get(&format!("{}.{}", parent.kind(), field)) {
                    return Some(w);
                }
            }
        }
        let mut up = node.parent();
        while let Some(ancestor) = up {
            if let Some(w) = self.by_key.get(&format!("{}/{}", ancestor.kind(), node.kind())) {
                return Some(w);
            }
            up = ancestor.parent();
        }
        self.by_key.get(node.kind()).map(String::as_str)
    }
}

/// A text parsed whole, so a chunk of it can be said from the tree.
struct Parsed {
    text: String,
    tree: Tree,
    /// character offset -> byte offset, plus the end
    byte_of: Vec<usize>,
    /// byte offset -> character offset, at character starts and the end
    char_of: HashMap<usize, usize>,
}

/// A grammar and the rules that say its nodes, over one parsed text.
pub struct Syntax {
    grammar: Arc<Grammar>,
    rules: Rules,
    parsed: Mutex<Option<Parsed>>,
}

impl Syntax {
    pub fn new(grammar: Arc<Grammar>, rules: &[(String, String)]) -> Self {
        Syntax { grammar, rules: Rules::new(rules), parsed: Mutex::new(None) }
    }

    /// Parse `text`, the whole document, for the chunks of it `found` is
    /// asked about.  False when the grammar could not parse it.
    pub fn parse(&self, text: &str) -> bool {
        let Some(tree) = self.grammar.parse(text) else { return false };
        let mut byte_of = Vec::new();
        let mut char_of = HashMap::new();
        for (c, (b, _)) in text.char_indices().enumerate() {
            byte_of.push(b);
            char_of.insert(b, c);
        }
        char_of.insert(text.len(), byte_of.len());
        byte_of.push(text.len());
        *self.parsed.lock().unwrap() = Some(Parsed { text: text.to_string(), tree, byte_of, char_of });
        true
    }

    /// Every node a rule says that starts within the chunk `text`, which
    /// starts at character `at` of the parsed text, as shapes are found:
    /// in chunk characters, with a piece per word slot of `slots` it covers.
    /// A node that runs past the chunk (a `def` with its body) is said as
    /// far as the chunk goes; the nodes inside one that began in an earlier
    /// chunk are said on their own.  Nothing unless `parse` was given a text
    /// the chunk is a slice of.
    pub fn found(&self, text: &str, at: usize, slots: &[(usize, usize)]) -> Vec<Found> {
        let guard = self.parsed.lock().unwrap();
        let Some(parsed) = guard.as_ref() else { return Vec::new() };
        let (Some(&lo), Some(&hi)) = (parsed.byte_of.get(at), parsed.byte_of.get(at + text.chars().count())) else {
            return Vec::new();
        };
        if parsed.text.get(lo..hi) != Some(text) {
            return Vec::new();
        }
        let mut out = Vec::new();
        let mut stack = vec![parsed.tree.root_node()];
        while let Some(node) = stack.pop() {
            if node.end_byte() <= lo || node.start_byte() >= hi {
                continue;
            }
            let starts_here = lo <= node.start_byte() && node.start_byte() < hi;
            if starts_here && node.start_byte() < node.end_byte() {
                if let Some(wording) = self.rules.wording(node) {
                    out.push(self.said(parsed, node, wording, at, slots, hi));
                    continue;
                }
            }
            let mut cursor = node.walk();
            let children: Vec<Node> = node.children(&mut cursor).collect();
            stack.extend(children.into_iter().rev());
        }
        out.sort_by_key(|f| (f.start, f.end));
        out
    }

    /// `node` said by `wording`, as a shape is found, up to byte `hi` of the
    /// parsed text where the chunk ends: what the wording says for text past
    /// it is left out.
    fn said(&self, p: &Parsed, node: Node, wording: &str, at: usize, slots: &[(usize, usize)], hi: usize) -> Found {
        let stop = node.end_byte().min(hi);
        let matched = &p.text[node.start_byte()..stop];
        let start = p.char_of[&node.start_byte()] - at;
        let end = p.char_of[&stop] - at;
        if wording.is_empty() {
            return Found { start, end, say: matched.to_string(), as_written: true, pieces: Vec::new() };
        }
        let mut pieces = Vec::new();
        self.expand(p, node, wording, node.start_byte(), 0, &mut pieces);
        let limit = stop - node.start_byte();
        pieces.retain_mut(|piece| {
            if piece.src >= limit {
                return false;
            }
            if piece.verbatim && piece.src + piece.text.len() > limit {
                piece.text.truncate(limit - piece.src);
            }
            piece.until = piece.until.min(limit);
            true
        });
        let say = crate::text::collapse(&pieces.iter().map(|x| x.text.as_str()).collect::<String>());
        let covered: Vec<(usize, usize)> =
            slots.iter().copied().filter(|&(a, b)| a < end && b > start).collect();
        let shares = shared(&pieces, matched, start, &covered);
        Found { start, end, say, as_written: false, pieces: shares }
    }

    /// `node` onto `out`: by its rule, or else its parts in order, the named
    /// ones by their rules and the rest as written, through `swaps`.  Byte
    /// offsets are from `base`, where the node a rule is saying starts.
    fn say_node(&self, p: &Parsed, node: Node, base: usize, depth: usize, out: &mut Vec<Piece>, swaps: &[(&str, &str)]) {
        if depth > MAX_DEPTH {
            swapped(&p.text[node.byte_range()], swaps, node.start_byte() - base, node.end_byte() - base, out);
            return;
        }
        match self.rules.wording(node) {
            Some("") => swapped(&p.text[node.byte_range()], swaps, node.start_byte() - base, node.end_byte() - base, out),
            Some(wording) => self.expand(p, node, wording, base, depth, out),
            None => self.say_parts(p, node, base, depth, out, false, swaps),
        }
    }

    /// `node`'s children in order, with the text between them as written;
    /// only the named children when `named_only`, comments and the like
    /// left out, and then nothing before the first or after the last.  The
    /// `swaps` touch the text said as written at this level, not the
    /// children's, which have rules of their own.  A word said for the text
    /// between two children (a comma as "and") may light any child after it.
    fn say_parts(&self, p: &Parsed, node: Node, base: usize, depth: usize, out: &mut Vec<Piece>, named_only: bool, swaps: &[(&str, &str)]) {
        let verbatim = |out: &mut Vec<Piece>, a: usize, b: usize| {
            if a < b {
                swapped(&p.text[a..b], swaps, a - base, node.end_byte() - base, out);
            }
        };
        let mut cursor = node.walk();
        let children: Vec<Node> = node.children(&mut cursor).collect();
        let part = |c: &Node| !named_only || (c.is_named() && !c.is_extra());
        let Some(first) = children.iter().find(|c| part(c)) else {
            if !named_only {
                verbatim(out, node.start_byte(), node.end_byte());
            }
            return;
        };
        let mut prev = if named_only { first.start_byte() } else { node.start_byte() };
        for child in &children {
            if part(child) {
                verbatim(out, prev, child.start_byte());
                if child.is_named() {
                    self.say_node(p, *child, base, depth + 1, out, &[]);
                } else {
                    verbatim(out, child.start_byte(), child.end_byte());
                }
                prev = child.end_byte();
            } else if named_only && child.is_extra() && child.start_byte() >= prev {
                // a comment among the parts: the text before it stays, it goes
                verbatim(out, prev, child.start_byte());
                prev = child.end_byte();
            }
        }
        if !named_only {
            verbatim(out, prev, node.end_byte());
        }
    }

    /// `wording` for `node` onto `out`: `$field` says the children of that
    /// field, `$0` the node in full, `$*` its named children with the text
    /// between them.  The `|from=to` replacements of a reference touch the
    /// text said as written at that level, between the parts and a field's
    /// own, never inside a part, which has rules of its own.  The wording's
    /// own words come from the text after the last field said.
    fn expand(&self, p: &Parsed, node: Node, wording: &str, base: usize, depth: usize, out: &mut Vec<Piece>) {
        let own = |out: &mut Vec<Piece>, text: &str, src: usize| {
            if !text.is_empty() {
                out.push(Piece { text: text.to_string(), src, verbatim: false, until: node.end_byte() - base });
            }
        };
        let word = |c: char| c.is_ascii_alphanumeric() || c == '_' || c == '*';
        // the wording's opening words come from the token right before the
        // node when there is one ("and returns type" lights the `->` before
        // a return type), else from the node's start
        let mut prev_end = node.start_byte() - base;
        if let Some(lead) = node.prev_sibling() {
            if !lead.is_named()
                && lead.start_byte() >= base
                && p.text[lead.end_byte()..node.start_byte()].trim().is_empty()
            {
                prev_end = lead.start_byte() - base;
            }
        }
        let mut rest = wording;
        while let Some(dollar) = rest.find('$') {
            own(out, &rest[..dollar], prev_end);
            let after = &rest[dollar + 1..];
            let (name, swaps, tail): (&str, Vec<(&str, &str)>, &str) = if let Some(tail) = after.strip_prefix('$') {
                own(out, "$", prev_end);
                rest = tail;
                continue;
            } else if let Some(inner) = after.strip_prefix('{') {
                let Some(close) = inner.find('}') else {
                    own(out, "$", prev_end);
                    rest = after;
                    continue;
                };
                let mut pieces = inner[..close].split('|');
                let name = pieces.next().unwrap_or("");
                let swaps = pieces
                    .filter_map(|f| f.split_once('='))
                    .filter(|(from, _)| !from.is_empty())
                    .collect();
                (name, swaps, &inner[close + 1..])
            } else {
                let n = after.find(|c: char| !word(c)).unwrap_or(after.len());
                if n == 0 {
                    own(out, "$", prev_end);
                    rest = after;
                    continue;
                }
                (&after[..n], Vec::new(), &after[n..])
            };
            let mut said: Vec<Piece> = Vec::new();
            match name {
                "0" => self.say_parts(p, node, base, depth, &mut said, false, &swaps),
                "*" => self.say_parts(p, node, base, depth, &mut said, true, &swaps),
                field => {
                    let mut cursor = node.walk();
                    let children: Vec<Node> = node.children_by_field_name(field, &mut cursor).collect();
                    let mut prev = None;
                    for child in children {
                        if let Some(prev) = prev {
                            swapped(&p.text[prev..child.start_byte()], &swaps, prev - base, node.end_byte() - base, &mut said);
                        }
                        self.say_node(p, child, base, depth + 1, &mut said, &swaps);
                        prev = Some(child.end_byte());
                    }
                }
            }
            if let Some(last) = said.last() {
                prev_end = last.until;
            }
            out.extend(said);
            rest = tail;
        }
        own(out, rest, prev_end);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_library_without_the_grammar_is_refused_by_name() {
        // the runtime's own test binary is a library of sorts; whatever is
        // loaded, nothing in it is called tree_sitter_nothing
        let here = std::env::current_exe().unwrap();
        let Err(err) = Grammar::load(&here, "nothing") else { panic!("loaded a grammar") };
        assert!(err.contains("nothing") || err.contains("cannot load"), "{err}");
    }

    #[test]
    fn a_missing_library_says_so() {
        let Err(err) = Grammar::load(Path::new("/nonexistent/python.so"), "python") else {
            panic!("loaded a grammar")
        };
        assert!(err.starts_with("cannot load /nonexistent/python.so"), "{err}");
    }

    #[test]
    fn a_null_grammar_is_refused() {
        assert!(unsafe { Grammar::from_raw(std::ptr::null()) }.is_err());
    }
}
