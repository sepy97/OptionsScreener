# The call ladder

Covered calls on shares that came from an assigned put, stepped up a fixed set of strikes (the
**rungs**) until the shares can leave at what they cost. Worked out on TER: 100 shares assigned at
$390 with TER near $270, a $340 call sold for about $6, and rungs at $340, $355, $370 and $390. The
Call ladder tab prices a plan like that against the chain; `core/ladder.py` holds the rules.

## Why fixed rungs

A strike picked by delta at roll time can't be ordered in advance. A fixed next rung can, so each
call carries two resting orders at the broker and needs a person only when they can't act.

The first rung is the worst exit: a jump past it strands the call there. Starting the rungs at a
floor (the $340 the TER plan would accept being called at) rather than at the share price is what
keeps a jump from locking in a large loss. Within each rung, the delta rises on its own as the
price approaches it: about 0.19 for the $340 call with TER at $300, 0.44 at $330.

## Planning the rungs

Rungs can be typed, or left empty and planned from the chain when the ladder is saved
(`plan_rungs`). They are kept from then on, since orders rest on them; clearing them plans them
again from that day's prices.

- **The first rung** is the strike of the call sold now. With none open, it is the strike at a
  delta set by how far the share price is under the real cost: 0.15 at 15% or more under it, 0.25
  within 5–15%, 0.40 closer. Far under the cost, the recovery is what's needed, so it isn't sold
  cheap. If that strike pays less than the minimum premium, the first rung is the highest strike
  below it that does.
- **The last rung**, the target, is the first strike at or above the cost.
- **The steps** in between are the fewest even ones, rounded to listed strikes, where every roll
  still pays the minimum roll credit at its worst: the trigger reached the day after a call is
  sold (45 days left), four weeks added, at volatility 10 points lower. That is the limit the roll
  order would carry, so each roll fills as soon as the trigger is reached.

On the TER plan, with TER at $269 and 68% volatility, the $340 call plans $340, $355, $375 and
$390. The lower the volatility, the less a roll pays for a step up and the more rungs it takes; at
two rolls a quarter, a ladder of many rungs takes a long time to climb. Steps that pay under the
minimum at their worst are marked on the page, typed or planned.

## The plan

The page runs the ladder's rules a day at a time on a path where the share price grows a steady
amount a month (1% unless the ladder says otherwise), at today's volatility, from the call open
now until the shares are called, the price passes the target, or four years go by:

- a call is bought back at its take-profit, rolled up a rung once the price reaches its trigger
  and the roll pays the order's limit, or held to expiry and called if the price is past the
  strike then;
- a new call is sold when the last one closes or expires, four to six weeks out and before the
  next report, at the lowest rung above both the price and its roll zone, and never below the
  rung reached. One that pays under the minimum waits a week;
- reports past the next one are taken to be a quarter apart, and expiries not listed yet to come
  on the usual Fridays: every Friday for a chain that lists weeklies, the third of each month for
  one that doesn't.

Calls sold again and again at one rung are one line of the plan. A steady path has none of the
stock's swings, so on it every call decays and every take-profit fills: the plan gives the order
of things and about when, and doesn't add up the premiums, which would be a best case.

## The two orders on each call

Linked so that one cancels the other: both buy back the same call, and two fills would leave a
long call.

- **Take profit**: buy to close at 25% of what the call was sold for, good till cancelled.
- **Roll up a rung**: a conditional order that waits for the share price to reach 97% of the
  strike, then buys the call back and sells the next rung four to six weeks after its expiry
  (the shortest that fits), for a net credit, good till cancelled.

The roll's **limit** is 85% of the lowest credit the model gives at the trigger price over the
call's remaining life, with volatility 10 points lower than now, since a rally usually lowers it.
It is never below the minimum roll credit. Near the strike the credit grows as expiry nears; far
above it the credit falls to nothing, so past the **gap price** (where, with 10 days left, the
credit drops under the limit) the order is sent but can't fill.

## Reports

No option is held over an earnings report. The last listed expiry before a report is a wall: a
roll must land before the report, so the call that expires at the wall can't roll, and only its
take-profit rests. After the report the ladder starts again. That leaves room for about two rolls
between reports: the ladder climbs at most two rungs a quarter.

## Minimums

As shares of the price, so that they hold for a $30 stock as for a $300 one (the TER plan's $3.00
and $0.50 at $300 and $340):

- a new call has to pay 1% of the share price;
- a roll has to pay 0.15% of the strike, and never a debit.

## What the page can't see

The model prices are Black–Scholes at one volatility (the open call's), at mid prices, without
dividends. A resting order fills at the bid and ask, which is lower. The page reads what the
person types: after a fill, the plan's numbers (the new call, what it sold for, the premiums) are
updated by hand. Gaps past the roll window, early assignment, the report and fills are what need a
person.
