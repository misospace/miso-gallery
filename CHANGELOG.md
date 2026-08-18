# Changelog

## [1.0.0](https://github.com/misospace/miso-gallery/compare/0.1.19...1.0.0) (2026-08-18)


### ⚠ BREAKING CHANGES

* **deps:** Update dependency Pillow (11.3.0 → 12.3.0) ([#369](https://github.com/misospace/miso-gallery/issues/369))
* **deps:** Update dependency pytest (8.4.2 → 9.1.1) ([#370](https://github.com/misospace/miso-gallery/issues/370))

### Features

* **deps:** Update dependency Pillow (11.3.0 → 12.3.0) ([#369](https://github.com/misospace/miso-gallery/issues/369)) ([103dbab](https://github.com/misospace/miso-gallery/commit/103dbab663220d1feb1eb5d82a14317db9f8b3d9))
* **deps:** Update dependency pytest (8.4.2 → 9.1.1) ([#370](https://github.com/misospace/miso-gallery/issues/370)) ([13b84a1](https://github.com/misospace/miso-gallery/commit/13b84a10e2d20ca0d2b4f5384c2a65e18adfb061))


### Bug Fixes

* 401: Make /api/webhook/run reachable when authentication is enabled ([#411](https://github.com/misospace/miso-gallery/issues/411)) ([c1cc26e](https://github.com/misospace/miso-gallery/commit/c1cc26ed8868ac9a8ba8168c01bbe5316a93b4db)), closes [#401](https://github.com/misospace/miso-gallery/issues/401)
* **auth:** repair oidc token refresh ([#410](https://github.com/misospace/miso-gallery/issues/410)) ([2b2d464](https://github.com/misospace/miso-gallery/commit/2b2d46433cad325dde7f1d765f84e4868fc1ac3c))
* Ensure proper LRU eviction for folder cover cache ([#377](https://github.com/misospace/miso-gallery/issues/377)) ([5fa81ae](https://github.com/misospace/miso-gallery/commit/5fa81aea33524eb1a60c542067f2be0b1179dc26)), closes [#348](https://github.com/misospace/miso-gallery/issues/348)
* **release:** detect app-authored release PRs ([#397](https://github.com/misospace/miso-gallery/issues/397)) ([f8fdea9](https://github.com/misospace/miso-gallery/commit/f8fdea9b55719865ddfcd775e34dbd47fd4032a9))
* **release:** recover already-bumped unpublished versions ([#395](https://github.com/misospace/miso-gallery/issues/395)) ([88eb684](https://github.com/misospace/miso-gallery/commit/88eb6847cea471334d5b7c68aee4ba974f01c9c4))
* **release:** recover missing image tags ([#398](https://github.com/misospace/miso-gallery/issues/398)) ([e9d6fc9](https://github.com/misospace/miso-gallery/commit/e9d6fc958645072c55ade8fe828e873a6f443536)), closes [#0](https://github.com/misospace/miso-gallery/issues/0)
* **release:** restore image recovery workflow ([#399](https://github.com/misospace/miso-gallery/issues/399)) ([cfcbf27](https://github.com/misospace/miso-gallery/commit/cfcbf2739492d3ba2656455dd43bbe6b51a72e92))
* set gunicorn --timeout to 130s to accommodate webhook tasks up to 120s ([#393](https://github.com/misospace/miso-gallery/issues/393)) ([3970224](https://github.com/misospace/miso-gallery/commit/39702242b990ddb43c990540bc00c0c682c32e67)), closes [#385](https://github.com/misospace/miso-gallery/issues/385)


### Documentation

* issue contract for the autonomous loop (template + AGENTS.md) ([#396](https://github.com/misospace/miso-gallery/issues/396)) ([7b26526](https://github.com/misospace/miso-gallery/commit/7b265264bb6787e5c620d89f894425a7607bced7))
