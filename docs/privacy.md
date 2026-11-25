# Privacy

`ha-repl` is a developer tool with full access to a Home Assistant instance. Rhizomatics receives none of the
data it reads, but that data can include personal information, and when an agent uses it the data is sent on
to the agent's model provider. This page covers what it can reach, where that data goes, and how to keep the
exposure down.

## What Rhizomatics collects

Nothing. `ha-repl`, its Home Assistant server component and its agent plugin have no telemetry, analytics or
crash reporting, and send nothing to Rhizomatics or anyone else. The only network connections they make are to
the Home Assistant instance you point them at.

This documentation site counts page views with [GoatCounter](https://www.goatcounter.com/), which sets no
cookies and keeps no personal data. Downloading `ha-repl` from PyPI, the server component through HACS or the
plugin from GitHub is covered by those services' own privacy policies.

## What it can reach

With the access token you give it, `ha-repl` can read anything in Home Assistant that the token's user can,
and with an administrator's token, which [live mode](modes/live_mode.md) needs, that is everything:

- **Entity states and attributes**, which often hold names, email addresses, phone numbers and the state of
  people's homes: who is in, which doors are open, what's playing where.
- **People and their devices**: `person` entities, device trackers and their **locations**, with
  presence history.
- **The recorder's history** through [`sql`](sql.md): everything recorded over the retention period,
  including where people have been and when.
- **Configuration and config entries** through `hass`, which can include other services' **credentials**,
  such as API keys and passwords that integrations keep.
- **Changes**: service calls and changes to `hass` objects take effect on the instance straight away.

## Where the data goes

- **Typed by you** in the shell, results are shown in your terminal. The shells keep a history of what you
  type, not what comes back, in `~/.ha_repl_history` and `~/.ha_repl_api_history`; `export_csv()` writes
  query results to a file you choose.
- **Run by an agent** with `ha-repl exec`, as the agent plugin's skill does, every result comes back into the
  agent's context. That means it is sent to the agent's model provider (Anthropic, OpenAI, GitHub or another)
  and handled under that provider's terms, which may include logging and retention. Personal data in a result
  goes with it.

## Keeping the exposure down

- **Use a test instance for agent work** where you can, rather than the Home Assistant that runs your home,
  so there is no real personal data to reach.
- **Give `ha-repl` its own Home Assistant user and token**, so its access can be revoked on its own, and
  remove the token when the work is done.
- **Switch off what you don't need.** The server component has a switch for `hass` and one for `sql`, each
  with an optional time limit, so access can be opened for one piece of work without being left on (see
  [Limiting What Is Exposed](configuration/server_install.md#limiting-what-is-exposed)). With both off, the
  [API mode](modes/api_mode.md) still reaches states and registries through Home Assistant's standard API.
- **Keep queries narrow**: ask for the entities and columns the task needs, with a small row limit.
- **Keep the token out of shared places**: `HASS_TOKEN` in a `.env` file belongs in `.gitignore`, and the
  token never in an agent's prompt or a commit.

The agent plugin's skill tells agents to fetch only what a task needs, to summarise rather than copy out
personal data, never to show tokens or credentials, and to ask before reading people, locations or config
entries. An agent can still be told otherwise, so treat that as a safeguard, not a guarantee.

## Contact

Questions about privacy and `ha-repl`: open a [GitHub issue](https://github.com/rhizomatics/homeassistant-repl/issues),
or for anything sensitive, use [private vulnerability reporting](https://github.com/rhizomatics/homeassistant-repl/security/advisories/new).
