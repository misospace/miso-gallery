# Changelog

## [0.2.0](https://github.com/misospace/miso-gallery/compare/0.1.19...0.2.0) (2026-08-22)


### ⚠ BREAKING CHANGES

* **deps:** Update dependency Pillow (11.3.0 → 12.3.0) ([#369](https://github.com/misospace/miso-gallery/issues/369))
* **deps:** Update dependency pytest (8.4.2 → 9.1.1) ([#370](https://github.com/misospace/miso-gallery/issues/370))

### Features

* **deps:** Update dependency Pillow (11.3.0 → 12.3.0) ([#369](https://github.com/misospace/miso-gallery/issues/369)) ([103dbab](https://github.com/misospace/miso-gallery/commit/103dbab663220d1feb1eb5d82a14317db9f8b3d9))
* **deps:** Update dependency pytest (8.4.2 → 9.1.1) ([#370](https://github.com/misospace/miso-gallery/issues/370)) ([13b84a1](https://github.com/misospace/miso-gallery/commit/13b84a10e2d20ca0d2b4f5384c2a65e18adfb061))


### Bug Fixes

* 401: Make /api/webhook/run reachable when authentication is enabled ([#411](https://github.com/misospace/miso-gallery/issues/411)) ([c1cc26e](https://github.com/misospace/miso-gallery/commit/c1cc26ed8868ac9a8ba8168c01bbe5316a93b4db)), closes [#401](https://github.com/misospace/miso-gallery/issues/401)
* **auth:** exempt /health* from before_request auth gate ([#428](https://github.com/misospace/miso-gallery/issues/428)) ([69c2682](https://github.com/misospace/miso-gallery/commit/69c26822331a0e6da75117b0b241b18f9d52c7eb)), closes [#422](https://github.com/misospace/miso-gallery/issues/422)
* **auth:** repair oidc token refresh ([#410](https://github.com/misospace/miso-gallery/issues/410)) ([2b2d464](https://github.com/misospace/miso-gallery/commit/2b2d46433cad325dde7f1d765f84e4868fc1ac3c))
* Ensure proper LRU eviction for folder cover cache ([#377](https://github.com/misospace/miso-gallery/issues/377)) ([5fa81ae](https://github.com/misospace/miso-gallery/commit/5fa81aea33524eb1a60c542067f2be0b1179dc26)), closes [#348](https://github.com/misospace/miso-gallery/issues/348)
* **gallery:** bound the scan cache with an LRU max-entries cap ([#433](https://github.com/misospace/miso-gallery/issues/433)) ([f0be1e0](https://github.com/misospace/miso-gallery/commit/f0be1e01c3a8efe08e48986507f34f10bb27d9bc)), closes [#420](https://github.com/misospace/miso-gallery/issues/420)
* **llm-api:** enumerate beyond GALLERY_SCAN_LIMIT in /api/llm images, folders, and recent ([#431](https://github.com/misospace/miso-gallery/issues/431)) ([762f2ad](https://github.com/misospace/miso-gallery/commit/762f2ad6037ee8945f7fab6c485c9058b353a974)), closes [#423](https://github.com/misospace/miso-gallery/issues/423)
* **release:** detect app-authored release PRs ([#397](https://github.com/misospace/miso-gallery/issues/397)) ([f8fdea9](https://github.com/misospace/miso-gallery/commit/f8fdea9b55719865ddfcd775e34dbd47fd4032a9))
* **release:** recover already-bumped unpublished versions ([#395](https://github.com/misospace/miso-gallery/issues/395)) ([88eb684](https://github.com/misospace/miso-gallery/commit/88eb6847cea471334d5b7c68aee4ba974f01c9c4))
* **release:** recover missing image tags ([#398](https://github.com/misospace/miso-gallery/issues/398)) ([e9d6fc9](https://github.com/misospace/miso-gallery/commit/e9d6fc958645072c55ade8fe828e873a6f443536)), closes [#0](https://github.com/misospace/miso-gallery/issues/0)
* **release:** restore image recovery workflow ([#399](https://github.com/misospace/miso-gallery/issues/399)) ([cfcbf27](https://github.com/misospace/miso-gallery/commit/cfcbf2739492d3ba2656455dd43bbe6b51a72e92))
* **security:** refuse to ship the per-worker rate limiter by default ([#429](https://github.com/misospace/miso-gallery/issues/429)) ([63dc133](https://github.com/misospace/miso-gallery/commit/63dc133b2da4bc9cdcbcd1ae92161b7690c5915c)), closes [#419](https://github.com/misospace/miso-gallery/issues/419)
* **security:** restrict /view and /images routes to media files ([#430](https://github.com/misospace/miso-gallery/issues/430)) ([ef4f2fc](https://github.com/misospace/miso-gallery/commit/ef4f2fc5821a881240e814235e6a74d41a9e0d8c)), closes [#418](https://github.com/misospace/miso-gallery/issues/418)
* **security:** sort CSP integration imports ([#434](https://github.com/misospace/miso-gallery/issues/434)) ([c30b807](https://github.com/misospace/miso-gallery/commit/c30b807227d145f82b248755c8938c4751f0e108)), closes [#421](https://github.com/misospace/miso-gallery/issues/421)
* set gunicorn --timeout to 130s to accommodate webhook tasks up to 120s ([#393](https://github.com/misospace/miso-gallery/issues/393)) ([3970224](https://github.com/misospace/miso-gallery/commit/39702242b990ddb43c990540bc00c0c682c32e67)), closes [#385](https://github.com/misospace/miso-gallery/issues/385)


### Documentation

* issue contract for the autonomous loop (template + AGENTS.md) ([#396](https://github.com/misospace/miso-gallery/issues/396)) ([7b26526](https://github.com/misospace/miso-gallery/commit/7b265264bb6787e5c620d89f894425a7607bced7))
