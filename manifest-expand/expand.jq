# Expand `variants:` templates in a bootc-debs-builder manifest.
#
# Reads the manifest as JSON on stdin and writes it back with every template
# replaced by one plain package per variant. Everything downstream (wave
# planner, job matrix, build-one-deb lookup) therefore only ever sees ordinary
# packages and needs no variant awareness of its own.
#
# Template keys, all optional unless noted:
#   variants:            {<key>: <override>}  required. One package per key.
#   variant-name-prefix: string   name = prefix + key.       Default: <name>-
#   variant-env:         string   env var holding the key.   Default: VARIANT
#   variant-source:      string   path appended to sources, {variant} substituted
#   variant-deps:        [string] deps appended to deps,      {variant} substituted
#
# A variant override carries any normal package field (env, deps, sources,
# when, deb-glob, ...) and wins over the template's own value; env, sources and
# deps are appended to the template's, every other field replaces it.
# `{variant}` in any path, dep or `deb-glob` is substituted with the variant key. A key may
# be empty (name = prefix) so one variant keeps the template's plain name.
#
# Set ARCH (empty = keep every arch) to also drop the other arch's packages in
# the same pass: a package is kept when its when.arch is absent or equal.
#
# Set UNSIGNED_KERNEL (empty = keep everything) to "yes" or "no" to decide the
# packages marked `when: {unsigned-kernel: true}` (the -nosign kernel twins):
# they are kept only for "yes". Unmarked packages are always kept.
#
# Set GPU_DRIVER (empty = keep everything) to "no" to drop the packages marked
# `when: {gpu-driver: true}` (opt-in GPU userspace entries, e.g. the
# proprietary NVIDIA libraries): they are built only when the caller asks for
# them. Unmarked packages are always kept.
#
# KERNEL_ARM (unset = drop) gates the coded-but-disabled kernel variants
# (kernel-pve arm64): a package marked `when: {kernel-arm: true}` is kept
# only when the caller sets KERNEL_ARM=yes. Unmarked packages are always
# kept.

def subst($k): sub("\\{variant\\}"; $k);

def expand:
  . as $t
  | ($t["variant-name-prefix"] // ($t.name + "-")) as $prefix
  | ($t["variant-env"] // "VARIANT") as $env_key
  | ($t.sources // []) as $sources
  | ($t.deps // []) as $deps
  | ($t | del(.variants, .["variant-name-prefix"], .["variant-env"],
               .["variant-source"], .["variant-deps"])) as $base
  | $t.variants | to_entries[]
  | . as $v
  | $base
  | .name = ($prefix + $v.key)
  | .env = (($base.env // {}) + ($v.value.env // {}) + {($env_key): $v.key})
  | .sources = ($sources
      + (($v.value.sources // []) | map(subst($v.key)))
      + (if ($t["variant-source"] // "") == ""
         then [] else [($t["variant-source"] | subst($v.key))] end))
  | .deps = ($deps
      + (($v.value.deps // []) | map(subst($v.key)))
      + (($t["variant-deps"] // []) | map(subst($v.key))))
  | (if (.["deb-glob"] // "") == "" then . else .["deb-glob"] = (.["deb-glob"] | subst($v.key)) end)
  | . + ($v.value | del(.env, .sources, .deps));

[
  .packages[]
  | if has("variants") then expand else . end
  | select((env.ARCH // "") == "" or ((.when.arch // env.ARCH) == env.ARCH))
  | select((env.UNSIGNED_KERNEL // "") != "no"
           or ((.when["unsigned-kernel"] // false) | not))
  | select((env.GPU_DRIVER // "") != "no"
           or ((.when["gpu-driver"] // false) | not))
  | select((env.KERNEL_ARM // "") == "yes"
           or ((.when["kernel-arm"] // false) | not))
] | {packages: .}
