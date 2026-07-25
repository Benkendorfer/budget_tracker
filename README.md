# Budget tracker

- [Budget tracker](#budget-tracker)
  - [Overview](#overview)
  - [Technical details](#technical-details)
  - [Database schema](#database-schema)
  - [Data protection / security](#data-protection--security)

## Overview

This is a project for tracking personal budgets. The planned features are the following:

1. [Todo] Import transaction records exported by banks or credit cards.
2. [Todo] Auto-categorize transactions based on their source, description, and amount. Allow the user to override auto-categorization, and learn from the user's inputs.
3. [Todo] Produce visualizations of historical spending and income amounts.
4. [Todo] Produce projections of future balances.
5. [Todo] Display details in a command-line interface.
6. [Todo] Display details in a graphical interface if desired.
7. [Todo] Connect to bank / credit card / broker APIs to read the user's current financial state.

## Technical details

The project is mostly written in Python, preserving the possibility of using C++ for hot loops.

The software maintains a local SQL database for transaction records. The project uses `SQLAlchemy` to handle SQL easily within Python, and to maintain the possibility of future cloud hosting of the SQL database if desired.

For now, we use a command-line interface for user interactions. In the future, we hope to expand to a graphical interface.

## Database schema

Conventions:

- Money is stored as an integer count of the currency's minor unit (`value_minor`,
  signed, negative = outflow) to avoid floating-point rounding. The number of minor
  units per major unit is given by `currency.decimal_places` (e.g. 2 for USD, 0 for JPY).
- `budget.value_minor` and `recurring.value_minor` use the same signed convention as
  transactions (negative = expense/outflow, positive = income/inflow).
- The application has a single **base currency** (`app_config.base_currency_id`), which
  the user can change at any time. Budgets are always expressed in the base currency
  (there is no per-budget currency).
- `exchange_rate` records one row per currency per day: `rate` is the value of 1 unit of
  `currency_id` expressed in `base_currency_id` on `rate_date`. `base_currency_id` is
  stored on each row so changing the base currency never corrupts historical rates; the
  base currency itself has an implicit rate of 1. Non-base transactions are converted to
  base **at report time** using the rate on their `posted_date` — converted amounts are
  not stored, so all reports reflect the current base currency.
- Dates/times are stored in UTC as ISO-8601.
- A transaction's `category_id` is nullable (NULL = uncategorized). When
  `transaction_split` rows exist, the parent's `category_id` is NULL and the splits'
  `value_minor` sum to the parent's `value_minor`.
- `import_id` is nullable — NULL means the transaction was entered manually or synced
  from an API rather than imported from a file.
- Transfers between your own accounts share a `transfer_group_id` so both legs can be
  excluded from spending/income totals (avoids double-counting).
- `raw_description` preserves the bank's original text; `description` may be
  cleaned/normalized.
- Each transaction points to a `vendor` (the raw merchant string seen in the import).
  `vendor.vendor_name_id` is nullable: NULL means no override, so the raw `vendor.name`
  is the display name. Setting it points the vendor at a `vendor_name` row, which both
  gives a readable name and lets several raw vendors aggregate under one name. The
  **effective vendor name** (the override if present, else the raw name) is what the UI
  filters and groups by.
- `category_source` records how the category was assigned (`manual` / `rule` / `unset`);
  only `manual` labels are treated as ground truth when learning new rules.
  `categorized_by_rule_id` records which rule fired (NULL if none).
- Rules are applied in ascending `priority` order (most specific first) and skipped when
  `is_enabled` is false.
- Enum-like text columns use a fixed vocabulary, enforced with CHECK constraints:
  `budget.period` & `recurring.cadence` (`weekly`/`monthly`/`quarterly`/`yearly`),
  `rule.match_field` (`description`/`amount`/`account`), `rule.match_type`
  (`contains`/`equals`/`regex`), `category_source` (`manual`/`rule`/`unset`).
- Lookup `value` columns are UNIQUE (`account_type`, `currency`, `tag`, `vendor_name`,
  marked `UK`); `vendor.name` is UNIQUE; `category` is unique on `(parent_id, value)`.
  `exchange_rate` is unique on `(currency_id, base_currency_id, rate_date)`.
- Lookup tables (`account_type`, `currency`, `tag`, `vendor`, `vendor_name`) and pure
  join tables (`transaction_tag`) omit `created_at`/`updated_at`.

```mermaid
%%{init: {'theme':'base', 'themeVariables':{'background':'#ffffff','lineColor':'#ff5c5c'}}}%%
erDiagram
    account ||--o{ transaction : has
    account_type ||--o{ account : classifies
    currency ||--o{ account : denominates
    category |o--o{ transaction : classifies
    category |o--o{ category : "parent of"
    currency ||--o{ transaction : has
    category ||--o{ rule : "matched by"
    transaction ||--o{ transaction_tag : has
    tag ||--o{ transaction_tag : labels
    transaction ||--o{ transaction_split : "split into"
    category ||--o{ transaction_split : categorizes
    category ||--o{ budget : budgets
    account ||--o{ recurring : schedules
    category |o--o{ recurring : classifies
    currency ||--o{ recurring : denominates
    account ||--o{ import : "source of"
    import |o--o{ transaction : "imported in"
    rule |o--o{ transaction : categorized
    account ||--o{ balance_snapshot : "snapshot of"
    currency ||--o| app_config : "base of"
    currency ||--o{ exchange_rate : "priced"
    currency ||--o{ exchange_rate : "quoted in"
    vendor |o--o{ transaction : "vendor of"
    vendor_name |o--o{ vendor : overrides

    account_type {
        int id PK
        str value UK
    }
    account {
        int id PK
        str name
        int account_type_id FK
        int currency_id FK
        int opening_balance_minor
        date opening_date
        datetime created_at
        datetime updated_at
    }
    transaction {
        int id PK
        int account_id FK
        int category_id FK
        int currency_id FK
        int import_id FK
        int categorized_by_rule_id FK
        int vendor_id FK
        date posted_date
        str description
        str raw_description
        int value_minor
        int transfer_group_id
        str category_source
        str import_hash UK
        datetime created_at
        datetime updated_at
    }
    vendor {
        int id PK
        str name UK
        int vendor_name_id FK
    }
    vendor_name {
        int id PK
        str value UK
    }
    currency {
        int id PK
        str value UK
        str symbol
        int decimal_places
    }
    category {
        int id PK
        int parent_id FK
        str value
        datetime created_at
        datetime updated_at
    }
    rule {
        int id PK
        int category_id FK
        str match_field
        str match_type
        str pattern
        int priority
        bool is_enabled
        datetime created_at
        datetime updated_at
    }
    tag {
        int id PK
        str value UK
    }
    transaction_tag {
        int transaction_id PK, FK
        int tag_id PK, FK
    }
    transaction_split {
        int id PK
        int transaction_id FK
        int category_id FK
        int value_minor
    }
    budget {
        int id PK
        int category_id FK
        int value_minor
        str period
        date start_date
        date end_date
        datetime created_at
        datetime updated_at
    }
    recurring {
        int id PK
        int account_id FK
        int category_id FK
        int currency_id FK
        int value_minor
        str description
        str cadence
        date next_date
        date end_date
        bool is_active
        datetime created_at
        datetime updated_at
    }
    import {
        int id PK
        int account_id FK
        str source_file
        int row_count
        datetime imported_at
    }
    balance_snapshot {
        int id PK
        int account_id FK
        date snapshot_date
        int balance_minor
    }
    app_config {
        int id PK
        int base_currency_id FK
        datetime updated_at
    }
    exchange_rate {
        int id PK
        int currency_id FK
        int base_currency_id FK
        date rate_date
        decimal rate
    }
```

## Data protection / security

All user data is stored in the `data/` directory, which is `.gitignored`. Within that directory, all user data is stored locally.
