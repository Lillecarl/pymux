# Real tmux configuration files, each pinned to a commit of the repository
# that holds it: tmux's own example, oh-my-tmux, and the most starred
# dotfiles that carry one. `checks.pymux-tmux-corpus` reads each through
# pymux. Lillecarl/pyterm#1027.
#
# Fetched and not copied in: about half of these repositories carry no
# licence, and the export would publish whatever this repository holds.
#
# The name of each file is the repository's, with `__` for the slash, so a
# line of the report names where to look.
{
  lib,
  fetchurl,
  linkFarm,
}:
let
  configurations = [
    {
      repo = "tmux/tmux";
      rev = "7d7da0b3a26876e828b716cabf4f0e452c684579";
      path = "example_tmux.conf";
      hash = "sha256-CrfCg0IYDUhUNMPY4oZQih4ch6yaLI8tx4vPR3ot5Dc=";
    }
    {
      repo = "gpakosz/.tmux";
      rev = "58a3dcc0d718ec0fa1c0d5a2fddd640a1ad7a5b7";
      path = ".tmux.conf";
      hash = "sha256-MIUKLiPKFwemdehw6cRIlxU41I/Nji8mgaHwBP6dTOA=";
    }
    {
      repo = "thoughtbot/dotfiles";
      rev = "939a27005b2dd4ac1556e104fc91122f3aea0a6c";
      path = "tmux.conf";
      hash = "sha256-68Qwydhr7m1fm+6/HZV7yjO+bE66Ioag0vOrfHB7vqI=";
    }
    {
      repo = "craftzdog/dotfiles-public";
      rev = "86edf45783045c56cdc74bf0a83f2fffcb7be828";
      path = "dot_config/tmux/tmux.conf";
      hash = "sha256-97w/aL275/jLOIyZboXnmOYYumcI4POaxHLuMBhz3IQ=";
    }
    {
      repo = "omerxx/dotfiles";
      rev = "97b2213ad0e61c7a036290752e23497920978d55";
      path = "tmux/tmux.conf";
      hash = "sha256-RzKkUwnkI0agu/7sKRwgafTtut2W4IB5tGLrbQgj6vg=";
    }
    {
      repo = "ThePrimeagen/.dotfiles";
      rev = "602019e902634188ab06ea31251c01c1a43d1621";
      path = "tmux/.tmux.conf";
      hash = "sha256-OMHeOwrqRNCi32+WEvHKnoKutGuIGzhviCoNMs9kP84=";
    }
    {
      repo = "nicknisi/dotfiles";
      rev = "125ddb795a38af2563b59f3a68c0cb73eff46040";
      path = "config/tmux/tmux.conf";
      hash = "sha256-hpVGT2hB2ml7FCz16DE04df818hxqPe4dSrCERxM/CM=";
    }
    {
      repo = "samoshkin/tmux-config";
      rev = "95efd543846a27cd2127496b74fd4f4da94f4a31";
      path = "tmux/tmux.conf";
      hash = "sha256-dJJqDqO+gos6NwGdlgtLhy6nIkjhAuPbin0TjnYwrgU=";
    }
    {
      repo = "tony/tmux-config";
      rev = "40d4b0ce6fd750b6de2520f71857bfc4feb27315";
      path = ".tmux.conf";
      hash = "sha256-wWbwk03lPcHlAQkmJia94u98H9U04SkEC3qRmcy/Avw=";
    }
    {
      repo = "ShawnPana/smux";
      rev = "84734364206403a2e51b601d3ad8c85464fcdd17";
      path = ".tmux.conf";
      hash = "sha256-TTIt/X/UBGYFy/nBNnNAExpiOaQq0XO8yVWEoV4xU9Y=";
    }
    {
      repo = "mislav/dotfiles";
      rev = "6a4f7758926694086f8da691d254890c45efb9b5";
      path = "tmux.conf";
      hash = "sha256-kaSgARUy/FoGnHBdkAW2cVoN+S7Ca3BDfyFndO51xgU=";
    }
    {
      repo = "folke/dot";
      rev = "10526a848771bab9e9fbb98b5995c997f7ef92b6";
      path = "config/tmux/tmux.conf";
      hash = "sha256-s7pzrr8+5yo/AfNknpXL/58ySv2IyYAA+FGtTPx9G/U=";
    }
    {
      repo = "statico/dotfiles";
      rev = "91396d5e175174dde0b3fd9be95d5bb09106a89d";
      path = ".tmux.conf";
      hash = "sha256-9eYgJjJLOzUvhN5VQbtJ89jXtPm0bEdcO4UiTxu0YFg=";
    }
    {
      repo = "hamvocke/dotfiles";
      rev = "11691f3d15276db2ed83dc9a7b1461f422663c31";
      path = "tmux/.tmux.conf";
      hash = "sha256-mAZTf5JYMlYSQm8nwGxKM+5fbwpJAydVYi+7grclh64=";
    }
    {
      repo = "dreamsofcode-io/tmux";
      rev = "e78974dd34fc07c78297b1abb6692f91aabbc4a5";
      path = "tmux.conf";
      hash = "sha256-ucGvqOVQYkSu1lgIeZvsnTqn+u9i9lZ5w+svStNQpi0=";
    }
    {
      repo = "christoomey/dotfiles";
      rev = "63bd0d44c6ab0b91ce0ba10876a3104d3f810fe0";
      path = "tmux/tmux.conf";
      hash = "sha256-6LStgTcKyFp7U/XTxCZI23QBoeBWroXkWzO6FHp6Vqc=";
    }
    {
      repo = "joshmedeski/dotfiles";
      rev = "dd877b8a0bd550e0177e6b8d3e9f56053ccdd6ff";
      path = ".config/tmux/tmux.conf";
      hash = "sha256-06bQaZUEt3hImfRed2yfdawHiWxc2hQ8rseLbz1zXnY=";
    }
    {
      repo = "vmware-archive/tmux-config";
      rev = "dc297ffd4ea462d59f23618edd7544632e7c45d6";
      path = "tmux.conf";
      hash = "sha256-sDJFr6XDPUjYQpBa8D3+8ovjtyHck1d7tl7LuRW0W6I=";
    }
    {
      repo = "shfshanyue/tmux-config";
      rev = "6a226da1fa7d3347d7eb1bd470f32085e400eb19";
      path = ".tmux.conf";
      hash = "sha256-eZoBWFegkh33B66/9N9t1ZO4+yQ5IUllNcU7J1Kj9W0=";
    }
    {
      repo = "nviennot/tmux-config";
      rev = "60d08eda28bc2a18e899e8f2379701d8ecacc35a";
      path = "tmux.conf";
      hash = "sha256-QVYtB6g0viEigNaA10BJOMbXEPG92+KICx04zLqQ84Q=";
    }
    {
      repo = "leslie255/magicTmux";
      rev = "d4b5763eacab5ece942ce7be770a0884ca44e43f";
      path = "tmux.conf";
      hash = "sha256-JhbYXC+WnsjXlbT1ZDaGSPTCyKq6Tj9hS+0/hqT42ac=";
    }
    {
      repo = "jbnicolai/tmux";
      rev = "a4aed81545c7f444024d61cbb724e779868a538a";
      path = ".tmux.conf";
      hash = "sha256-InJu2yIRIKlg40oPVjuhgaukvdd/yUJh2BKeAR5NxwM=";
    }
    {
      repo = "tedsluis/tmux.conf";
      rev = "397b8585c143c87c713fcc06d5c93be92131a156";
      path = ".tmux.conf";
      hash = "sha256-u0YFNCtBcsXBm1mvKbXneFdaOLpnNPU+Wv51BpoSTt8=";
    }
    {
      repo = "jmbuhr/tmux-kickstarter";
      rev = "a612362a6f315caf251af4a62550bdd56a227c5c";
      path = ".tmux.conf";
      hash = "sha256-WorkcqSBON+e4Jhhvkz8ZWBz5EFbF/kWAakOlPkhad0=";
    }
    {
      repo = "AdamWhittingham/tmux-config";
      rev = "6535bd4de927be305d07aeefbe88ff1723de5af4";
      path = "tmux.conf";
      hash = "sha256-hrej05BNTEwWoJj0m+JDjgSWz+Z5ObPUb5tvEYRxTic=";
    }
    {
      repo = "edouard-lopez/tmux-config";
      rev = "07fbc36488337c8563fd881d3bbc2c44fa5d96fc";
      path = ".tmux.conf";
      hash = "sha256-VUxaz0vTbsWT40LpvCnZwecQ/fTopGX9NO+aC5sqY7c=";
    }
  ];

  nameOf = one: "${lib.replaceStrings [ "/" ] [ "__" ] one.repo}.conf";
in
linkFarm "tmux-corpus" (
  map (one: {
    name = nameOf one;
    path = fetchurl {
      name = nameOf one;
      url = "https://raw.githubusercontent.com/${one.repo}/${one.rev}/${one.path}";
      inherit (one) hash;
    };
  }) configurations
)
