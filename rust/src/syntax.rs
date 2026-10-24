//! Source code as a syntax tree: the tree-sitter runtime, with each
//! language's grammar loaded at run time rather than compiled in.
//!
//! readaloud ships no grammar.  A grammar is a `TSLanguage` that the
//! grammar's own code hands out: the `tree-sitter-<language>` wheel on PyPI
//! gives it as a `tree_sitter.Language` capsule, and a `parser/<language>.so`
//! as Neovim builds them exports a function `tree_sitter_<language>` that
//! returns it.  Either way the runtime checks the grammar's ABI version
//! before it parses with it, as Neovim does.

use std::ffi::c_void;
use std::path::Path;

use tree_sitter::ffi::TSLanguage;
use tree_sitter::{Language, Parser, Tree, LANGUAGE_VERSION, MIN_COMPATIBLE_LANGUAGE_VERSION};

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
