# Changelog

## [0.2.1](https://github.com/misospace/miso-gallery/compare/0.2.0...0.2.1) (2026-09-05)


### Bug Fixes

* **auth:** resolve OIDC token refresh URL from discovery metadata ([545c36a](https://github.com/misospace/miso-gallery/commit/545c36a07aed62ceabd4593dbb7bba3fa028abf0))
* **auth:** resolve OIDC token refresh URL from discovery metadata ([69d640a](https://github.com/misospace/miso-gallery/commit/69d640a4da879e540c023328817114c9ea7c7dcc)), closes [#450](https://github.com/misospace/miso-gallery/issues/450)
* **recent:** compare scan truncation against the enumeration bound actually used ([b91e9ad](https://github.com/misospace/miso-gallery/commit/b91e9ad50555c1a29ba737f379ed80383d8fd7ce))
* **recent:** truthful truncation banner + single-lstat bounded recent scan ([5e14f50](https://github.com/misospace/miso-gallery/commit/5e14f50e4d4ed0b1b34bdab26365477a1ec7131c))
* **security:** exclude dotfiles and symlinks from index() listing ([a9c31d0](https://github.com/misospace/miso-gallery/commit/a9c31d0a8a6d0216a975fcf8a25f5e4e812ee132))
* **security:** exclude dotfiles and symlinks from index() listing ([5b86765](https://github.com/misospace/miso-gallery/commit/5b86765bcf84030e8b79b19ecec6bf9aac328be1)), closes [#444](https://github.com/misospace/miso-gallery/issues/444)
* **security:** make /auth/oidc/refresh POST-only with CSRF and rate limiting ([6e8aa05](https://github.com/misospace/miso-gallery/commit/6e8aa0549f428b75d633ae937c7af9d489cc6d15))
* **security:** make /auth/oidc/refresh POST-only with CSRF and rate limiting ([b560358](https://github.com/misospace/miso-gallery/commit/b5603581559b4d0ae8076a13b34a9cef9239b6d8)), closes [#454](https://github.com/misospace/miso-gallery/issues/454)
* **security:** rate-limit the OIDC routes to stop provider spam ([218d0f2](https://github.com/misospace/miso-gallery/commit/218d0f243b9bc52ad65a496c3bc32c1bf3d55129))
* **security:** rate-limit the OIDC routes to stop provider spam ([7339aff](https://github.com/misospace/miso-gallery/commit/7339aff2e99e63dc873d1c705d490dbb22f771a3)), closes [#446](https://github.com/misospace/miso-gallery/issues/446)
* **security:** reject backslash and dot-prefixed open redirect URLs ([6fe09b8](https://github.com/misospace/miso-gallery/commit/6fe09b8c81159489f46c3dca1387c8c6b288b6dd))
* **security:** reject backslash and dot-prefixed open redirect URLs ([1f5894d](https://github.com/misospace/miso-gallery/commit/1f5894d89ae31e4de7f518ebf68419f19e443d11)), closes [#452](https://github.com/misospace/miso-gallery/issues/452)
* **service-worker:** stop caching authenticated navigation responses and purge caches on auth-state change ([f7ac88e](https://github.com/misospace/miso-gallery/commit/f7ac88e51f3493d7e63283500eee88caf5ec2d76))
* **service-worker:** stop caching authenticated navigation responses and purge caches on auth-state change ([3ff7f05](https://github.com/misospace/miso-gallery/commit/3ff7f0503ea3e45d18cdc0f38d84dd868c4250bc)), closes [#453](https://github.com/misospace/miso-gallery/issues/453)
* **thumbnails:** skip symlinks in integrity check and batch thumbnail removal ([dcea0f3](https://github.com/misospace/miso-gallery/commit/dcea0f36d9b36660d1c9ab1c8766eab90cfec33b))
* **thumbnails:** skip symlinks in integrity check and batch thumbnail removal ([d0ba421](https://github.com/misospace/miso-gallery/commit/d0ba421d4c5b115aea9a429d6d70c6e1dc19e20b)), closes [#445](https://github.com/misospace/miso-gallery/issues/445)
* **trash:** refuse to restore trashed items containing symlinks ([77f066a](https://github.com/misospace/miso-gallery/commit/77f066abb0df9b81f63742a606f561bc466f0c13))
* **trash:** refuse to restore trashed items containing symlinks ([1097e45](https://github.com/misospace/miso-gallery/commit/1097e4527b7c383dd4039bf3e0fa6dbaf382b2b5)), closes [#455](https://github.com/misospace/miso-gallery/issues/455)
* **webhook:** cap concurrent task invocations per worker ([d403b61](https://github.com/misospace/miso-gallery/commit/d403b616ca89421df6e836ea234ee4ba220a251c))
* **webhook:** cap concurrent task invocations per worker ([be33796](https://github.com/misospace/miso-gallery/commit/be33796f60ca1ac2b4117c96933b42ce42e5cf2a)), closes [#448](https://github.com/misospace/miso-gallery/issues/448)


### Performance Improvements

* **recent:** single-lstat bounded walk with heap selection and result caching ([f2b6d84](https://github.com/misospace/miso-gallery/commit/f2b6d84eff0474854d3cdad9cc481cd47cb24c46))

## [0.2.0](https://github.com/misospace/miso-gallery/compare/0.1.19...0.2.0) (2026-09-02)


### ⚠ BREAKING CHANGES

* **deps:** Update dependency Pillow (11.3.0 → 12.3.0) ([#369](https://github.com/misospace/miso-gallery/issues/369))
* **deps:** Update dependency pytest (8.4.2 → 9.1.1) ([#370](https://github.com/misospace/miso-gallery/issues/370))

### Features

* **deps:** Update dependency Pillow (11.3.0 → 12.3.0) ([#369](https://github.com/misospace/miso-gallery/issues/369)) ([103dbab](https://github.com/misospace/miso-gallery/commit/103dbab663220d1feb1eb5d82a14317db9f8b3d9))
* **deps:** Update dependency pytest (8.4.2 → 9.1.1) ([#370](https://github.com/misospace/miso-gallery/issues/370)) ([13b84a1](https://github.com/misospace/miso-gallery/commit/13b84a10e2d20ca0d2b4f5384c2a65e18adfb061))
* **deps:** update dependency authlib (1.7.2 → 1.8.0) ([#441](https://github.com/misospace/miso-gallery/issues/441)) ([d403aae](https://github.com/misospace/miso-gallery/commit/d403aae4ff5eb6836bfa40e81149690d9c57157c))
* **deps:** update dependency gunicorn (26.1.0 → 26.2.0) ([#437](https://github.com/misospace/miso-gallery/issues/437)) ([8435dde](https://github.com/misospace/miso-gallery/commit/8435dde9bc8955d096859b3415a7aa18c8d7cf04f))


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
* **recent:** enumerate beyond GALLERY_SCAN_LIMIT on /recent ([#438](https://github.com/misospace/miso-gallery/issues/438)) ([8b22f9b](https://github.com/misospace/miso-gallery/commit/8b22f9bb781a24f2daffce1f4ee7ecab23e42fad)), closes [#436](https://github.com/misospace/miso-gallery/issues/436)
* **security:** reject symlinks resolving outside DATA_FOLDER in media routes ([#456](https://github.com/misospace/miso-gallery/issues/456)) ([719f201](https://github.com/misospace/miso-gallery/commit/719f2010627af6ab6e4240ae296eecc6119c7b89)), closes [#442](https://github.com/misospace/miso-gallery/issues/442)


### Documentation

* issue contract for the autonomous loop (template + AGENTS.md) ([#396](https://github.com/misospace/miso-gallery/issues/396)) ([7b26526](https://github.com/misospace/miso-gallery/commit/7b265264bb6787e5c620d89f894425a7607bced7))
