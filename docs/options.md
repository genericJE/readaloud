# Options and voices

Every command line flag readaloud takes, and the voices it can speak in.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `TEXT...` | | text to read, joined with spaces |
| `-f`, `--file FILE` | | read `FILE` instead of stdin (`-` means stdin); a lone `TEXT` that names a file is read as that file too. The file's extension picks its configs and grammar through `[files]`, see [File types](configuration.md#file-types) |
| `--conf NAME` | | read the input with the configs for a `.NAME` file (`--conf md`), or with the config `NAME` (`--conf python`), whatever it is called: a pipe of Markdown, a `.txt` of Python |
| `-v`, `--voice NAME` | `af_heart` | Kokoro voice (see below) |
| `-s`, `--speed X` | `1.0` | speech rate multiplier, 0.5 to 3.0 in the TUI |
| `--sentences N` | `4` | max sentences per chunk |
| `--chars N` | `380` | max characters per chunk |
| `--prefetch N` | `2` | chunks synthesized ahead of the current one |
| `--device DEV` | system default | output device: index, name substring, or `default` |
| `--start N` | `1` | begin playback at the Nth speakable chunk |
| `--save FILE.wav` | | render everything to a WAV and exit |
| `--lang CODE` | from the voice | Kokoro language code (`a`, `b`, `j`, …) |
| `--repo ID` | `mlx-community/Kokoro-82M-4bit` | model repo |
| `--color` / `--no-color` | colour on | render the input's colours, and a source file's by its grammar, or force monochrome |
| `--media-keys` / `--no-media-keys` | on | take over the system play/pause button, or leave it alone |
| `--config PATH` | `~/.readaloud/` | read the preferences from `PATH`, a directory or a single `.conf` file, instead |
| `--no-config` | | ignore the preferences; built-in defaults only |
| `--write-config` | | write the shipped files (`readaloud.conf`, `markdown.conf`, `python.conf`, overwriting) and exit |
| `--list-voices` | | print the voices for the chosen language |
| `--tree` | | print the syntax tree of `FILE` through its grammar, the node kinds and fields a `[syntax]` rule can name, and exit (see [Syntax](pronunciations.md#syntax)) |
| `--list-devices` | | print the audio output devices |

The "Default" column is the *built-in* default. Anything set in
[`~/.readaloud/`](configuration.md) wins over it, and an explicit flag wins over both.

## Voices

`--list-voices` prints the ones your language code can use. The language is derived from
the first letter of the voice name unless you pass `--lang`.

| Language | Voices |
| --- | --- |
| American English (`a`) | `af_alloy` `af_aoede` `af_bella` `af_heart` `af_jessica` `af_kore` `af_nicole` `af_nova` `af_river` `af_sarah` `af_sky` `am_adam` `am_echo` `am_eric` `am_fenrir` `am_liam` `am_michael` `am_onyx` `am_puck` `am_santa` |
| British English (`b`) | `bf_alice` `bf_emma` `bf_isabella` `bf_lily` `bm_daniel` `bm_fable` `bm_george` `bm_lewis` |
| Spanish (`e`) | `ef_dora` `em_alex` `em_santa` |
| French (`f`) | `ff_siwis` |
| Hindi (`h`) | `hf_alpha` `hf_beta` `hm_omega` `hm_psi` |
| Italian (`i`) | `if_sara` `im_nicola` |
| Japanese (`j`) | `jf_alpha` `jf_gongitsune` `jf_nezumi` `jf_tebukuro` `jm_kumo` |
| Portuguese (`p`) | `pf_dora` `pm_alex` `pm_santa` |
| Mandarin (`z`) | `zf_xiaobei` `zf_xiaoni` `zf_xiaoxiao` `zf_xiaoyi` `zm_yunjian` `zm_yunxi` `zm_yunxia` `zm_yunyang` |

`af_heart` is the default and the best-behaved. Non-English languages need misaki's extra
language packs, which this project does not install by default.

[Back to the README](../README.md)
