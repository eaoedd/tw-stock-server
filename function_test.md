# Function Test

## 1. Register and Log In

**Steps**
1. Open `http://localhost:8000`
2. Click "Create one" on the login modal
3. Enter a username and password, confirm password, click "Create Account"
4. Verify redirected to main app with username shown in watchlist badge
5. Refresh the page — verify auto-login restores the session

**Expected**
- Account created successfully
- Username badge shows correct lowercase username
- Session persists after page refresh

**Result** — PASS (automated)
- Account created via `POST /users/{username}` ✅
- Login returns valid token via `POST /login` ✅
- Session valid: authenticated watchlist fetch succeeds ✅

---

## 2. Add Trades — Linked TWD Cash Trades Created and Deleted Together

**Steps**
1. Switch to Trades session, click "+ New Trade"
2. Add a **buy** trade (e.g. 0050, 100 shares, price 150, fee 100)
3. Verify a second row appears automatically: TWD withdraw for `100 × 150 + 100 = 15,100`
4. Add a **sell** trade (e.g. 0050, 50 shares, price 160, tax 240, fee 60)
5. Verify a second row appears automatically: TWD deposit for `50 × 160 − 240 − 60 = 7,700`
6. Delete the buy trade — confirm dialog mentions the linked cash trade
7. Verify both the buy row and its TWD withdraw row are removed

**Expected**
- Every buy creates a paired TWD withdraw; every sell creates a paired TWD deposit
- Deleting a stock trade removes its linked cash trade in the same operation

**Result** — PASS (automated)
- Buy trade created with correct id ✅
- Linked TWD withdraw created with correct amount (15,100) ✅
- Both trades exist after creation ✅
- `DELETE /trades/{id}` cascades to linked cash trade ✅
- Neither trade remains after cascade delete ✅

---

## 3. Edit a Trade — Linked Cash Trade Updates

**Steps**
1. Click ✎ on an existing buy trade
2. Change the price (e.g. 150 → 160) and click "Save"
3. Verify the trade row updates to the new price
4. Verify the linked TWD withdraw row updates its amount accordingly (`shares × new_price + fee`)

**Expected**
- Edited trade reflects new values
- Linked TWD cash trade amount recalculates automatically

**Result** — PASS (automated)
- `PUT /trades/{username}/{id}` updates price to new value ✅
- Linked TWD cash trade amount recalculated correctly (5,050 → 6,050) ✅

---

## 4. In Stock — Positions, Pie Chart, FIFO Lot Expansion

**Steps**
1. Switch to "In Stock" session
2. Verify the table shows all held tickers with correct shares, avg cost, current price, market value, unrealized P&L and unrealized %
3. Verify the TWD row reflects current cash balance
4. Verify the pie chart slices match the relative market values of each position
5. Click a ticker that has multiple buy trades — verify it expands to show individual FIFO lots (date, shares, cost per share, lot total cost)
6. Click again to collapse

**Expected**
- All positions calculated correctly via FIFO
- Pie chart proportions match market values
- Lot breakdown shows correct remaining shares per lot

**Result** — Pending manual browser verification

---

## 5. Run Simulation with Multiple Portfolios

**Steps**
1. Switch to "Simulation" session
2. Enter allocations for Portfolio 1 (e.g. `0050:60, 00675L:40`) and Portfolio 2 (e.g. `2330:100`)
3. Change the period (e.g. 1Y → 2Y) — verify chart updates automatically
4. Click "+ Portfolio", add a third portfolio, verify a new colored line appears
5. Verify the Statistics table shows Ann. Return, Ann. Vol, Sharpe, Max DD for each portfolio
6. Verify the Correlation matrix dimensions match the number of portfolios

**Expected**
- One colored line per portfolio on the chart
- Statistics and correlation update whenever portfolios or period change
- Up to 10 portfolios supported

**Result** — Pending manual browser verification

---

## 6. Toggle EN / 中文

**Steps**
1. Click "中文" button in the top-right of the navbar
2. Verify all UI text switches to Traditional Chinese: nav tabs, watchlist label, form labels, table headers, action names (買入/賣出/存款/提款), footer labels, auth modal
3. Click "EN" to switch back
4. Refresh the page — verify language preference is restored

**Expected**
- All visible text updates immediately on toggle
- Preference persists across page reloads via localStorage

**Result** — Pending manual browser verification

---

## 7. Backup, Delete a Trade, Restore

**Steps**
1. Click the username badge to open the Account modal
2. Click "⬇ Backup" — verify a `{username}_backup.json` file is downloaded
3. Open the file and confirm it contains `trades`, `watchlist`, and `portfolios` with correct data
4. Delete one trade from the Trades session
5. Return to Account modal, click "⬆ Restore", select the downloaded backup file
6. Confirm the dialog prompt, verify the status line shows restored counts
7. Verify the deleted trade reappears in the Trades session

**Expected**
- Backup file contains all trades, watchlist tickers, and simulation portfolios
- Restore overwrites server data and immediately reloads the UI
- All data matches the state at backup time

**Result** — PASS (automated)
- `GET /users/{username}/export` returns valid JSON ✅
- Backup contains watchlist (2 tickers) ✅
- Backup contains trades (2 trades) ✅
- All trades deleted before restore ✅
- `POST /users/{username}/import` restores all data ✅
- Trades count matches after restore ✅
- Watchlist count matches after restore ✅
