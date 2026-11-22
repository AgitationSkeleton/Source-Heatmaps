-- Source Heatmaps schema (MySQL 5.7+ / MariaDB 10.3+)
--
-- Game servers can only INSERT into the two staging tables (hm_ingest, hm_ingest_meta).
-- The exporter validates staging rows, folds them into the aggregate tables and deletes them.
-- Nothing on the website writes here; the site is static files the exporter generates.

CREATE DATABASE IF NOT EXISTS `heatmap` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `heatmap`;

-- ---- staging (append-only for game servers) ----

-- One row = one aggregated bucket from one plugin flush.
-- kind: 0 grounded presence, 1 death, 2 movement edge, 3 airborne presence.
-- x/y/z: cell indices (floor(world / cell)); presence/death cells are 32 units, edges 64 units.
-- x2/y2/z2 are only used by edges (kind 2).
-- cls: canonical class (see docs/DATA_FORMAT.md); team: the game's team index; bot: 0 human, 1 bot.
CREATE TABLE IF NOT EXISTS `hm_ingest` (
  `id`       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `received` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `port`     SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  `game`     VARCHAR(32) NOT NULL,
  `map`      VARCHAR(96) NOT NULL,
  `kind`     TINYINT UNSIGNED NOT NULL,
  `team`     TINYINT UNSIGNED NOT NULL,
  `cls`      TINYINT UNSIGNED NOT NULL,
  `bot`      TINYINT UNSIGNED NOT NULL,
  `x`        SMALLINT NOT NULL,
  `y`        SMALLINT NOT NULL,
  `z`        SMALLINT NOT NULL,
  `x2`       SMALLINT NOT NULL DEFAULT 0,
  `y2`       SMALLINT NOT NULL DEFAULT 0,
  `z2`       SMALLINT NOT NULL DEFAULT 0,
  `n`        INT UNSIGNED NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;

-- Per-map metadata from a server. kind 1 = team name (k = team index, v = name).
CREATE TABLE IF NOT EXISTS `hm_ingest_meta` (
  `id`       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `received` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `port`     SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  `game`     VARCHAR(32) NOT NULL,
  `map`      VARCHAR(96) NOT NULL,
  `kind`     TINYINT UNSIGNED NOT NULL,
  `k`        INT NOT NULL,
  `v`        VARCHAR(64) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;

-- ---- aggregates (exporter only) ----

CREATE TABLE IF NOT EXISTS `hm_cell` (
  `game` VARCHAR(32) NOT NULL,
  `map`  VARCHAR(96) NOT NULL,
  `kind` TINYINT UNSIGNED NOT NULL,
  `team` TINYINT UNSIGNED NOT NULL,
  `cls`  TINYINT UNSIGNED NOT NULL,
  `bot`  TINYINT UNSIGNED NOT NULL,
  `x`    SMALLINT NOT NULL,
  `y`    SMALLINT NOT NULL,
  `z`    SMALLINT NOT NULL,
  `n`    BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (`game`,`map`,`kind`,`team`,`cls`,`bot`,`x`,`y`,`z`)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `hm_edge` (
  `game` VARCHAR(32) NOT NULL,
  `map`  VARCHAR(96) NOT NULL,
  `team` TINYINT UNSIGNED NOT NULL,
  `cls`  TINYINT UNSIGNED NOT NULL,
  `bot`  TINYINT UNSIGNED NOT NULL,
  `x`    SMALLINT NOT NULL,
  `y`    SMALLINT NOT NULL,
  `z`    SMALLINT NOT NULL,
  `x2`   SMALLINT NOT NULL,
  `y2`   SMALLINT NOT NULL,
  `z2`   SMALLINT NOT NULL,
  `n`    BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (`game`,`map`,`team`,`cls`,`bot`,`x`,`y`,`z`,`x2`,`y2`,`z2`)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `hm_team` (
  `game` VARCHAR(32) NOT NULL,
  `team` TINYINT UNSIGNED NOT NULL,
  `name` VARCHAR(64) NOT NULL,
  PRIMARY KEY (`game`,`team`)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `hm_map` (
  `game`       VARCHAR(32) NOT NULL,
  `map`        VARCHAR(96) NOT NULL,
  `first_seen` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_seen`  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `samples`    BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `dirty`      TINYINT UNSIGNED NOT NULL DEFAULT 1,
  PRIMARY KEY (`game`,`map`)
) ENGINE=InnoDB;
