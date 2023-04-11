import json
import os
from datetime import datetime

from ibflex import FlexQueryResponse, BuySell

import LoggerFactory
from GhostfolioApi import GhostfolioApi
from IbkrApi import IbkrApi

write_debug_files = os.environ.get("WRITE_DEBUG_FILES", "FALSE")
logger = LoggerFactory.get_logger(__name__)


def get_cash_amount_from_flex(query):
    cash = 0
    try:
        cash += query.FlexStatements[0].CashReport[0].endingCash
    except Exception as e:
        print(e)
    try:
        cash += query.FlexStatements[0].CashReport[0].endingCashPaxos
    except Exception as e:
        print(e)
    return cash


def format_act(act):
    symbol_nested = act.get("SymbolProfile", {"symbol": ""}).get("symbol")
    return {
        "accountId": act["accountId"],
        "date": act["date"][0:18],
        "fee": float(act["fee"]),
        "quantity": act["quantity"],
        "symbol": act.get("symbol", symbol_nested),
        "type": act["type"],
        "unitPrice": act["unitPrice"],
    }


def is_act_present(act_search, acts):
    # if sync has written id as comment,
    # extract it, it should could be the transaction id
    comment = act_search["comment"]

    for act in acts:
        if comment is not None and comment.startswith(act["comment"]):
            return True
        act1 = format_act(act)
        act2 = format_act(act_search)
        if act1 == act2:
            return True
    return False


def get_diff(old_acts, new_acts):
    diff = []
    for new_act in new_acts:
        if not is_act_present(new_act, old_acts):
            diff.append(new_act)
    return diff


class SyncIBKR:
    IBKRCATEGORY = "9da3a8a7-4795-43e3-a6db-ccb914189737"

    def __init__(self, ghost_host, ibkrtoken, ibkrquery, ghost_token, ghost_currency):
        self.ghostfolio_api = GhostfolioApi(ghost_host, ghost_token, ghost_currency)
        self.ibkr_api = IbkrApi(ibkrtoken, ibkrquery)
        self.ghost_currency = ghost_currency

    def sync_ibkr(self):
        account_id = self.ghostfolio_api.create_or_get_ibkr_account_id()
        if account_id == "":
            logger.warning("Failed to retrieve account ID closing now")
            return
        query: FlexQueryResponse = self.ibkr_api.get_and_parse_query()
        activities = []
        date_format = "%Y-%m-%d"

        self.set_cash_to_account(account_id, get_cash_amount_from_flex(query))
        skipped_categories_counter = {}
        for trade in query.FlexStatements[0].Trades:

            if trade.assetCategory is not trade.assetCategory.STOCK:
                logger.debug("ignore %s: %s", trade.assetCategory, trade.symbol)
                existing_skips = skipped_categories_counter.get(trade.assetCategory,0)
                skipped_categories_counter[trade.assetCategory] = existing_skips + 1
                continue

            if trade.openCloseIndicator is None:
                logger.warning("trade is not open or close (ignoring): %s", trade)

            date = datetime.strptime(str(trade.tradeDate), date_format)
            iso_format = date.isoformat()
            symbol = trade.symbol
            if ".USD-PAXOS" in trade.symbol:
                symbol = trade.symbol.replace(".USD-PAXOS", "") + "USD"

            if trade.buySell == BuySell.BUY:
                buy_sell = "BUY"
            else:
                buy_sell = "SELL"

            data_source, yahoo_symbol, yahoo_currency = self.ghostfolio_api.get_ticker(
                trade.isin,
                symbol)
            unit_price = float(trade.tradePrice)
            unit_currency = trade.currency
            fee = float(trade.taxes)

            # Handling special case:
            # ghostfolio is checking currency against source (yahoo)
            if trade.currency != yahoo_currency:
                # converting GBP to GBp (IB vs Yahoo)
                if trade.currency == 'GBP' and yahoo_currency == 'GBp':
                    logger.debug("Converting GBP to GBp for Yahoo compatibility")
                    unit_price *= 100
                    unit_currency = 'GBp'
                    if trade.ibCommissionCurrency == 'GBP':
                        fee += float((trade.ibCommission * 100) * -1)
            else:
                fee += float(trade.ibCommission * -1)

            activities.append({
                "accountId": account_id,
                "currency": unit_currency,
                "dataSource": data_source,
                "date": iso_format,
                "fee": fee,
                "quantity": abs(float(trade.quantity)),
                "symbol": yahoo_symbol,
                "type": buy_sell,
                "unitPrice": unit_price,
                "comment": f"<sync-trade-transactionID>"
                           f"{trade.transactionID}"
                           f"</sync-trade-transactionID>",
            })

        if len(skipped_categories_counter) > 0:
            logger.info(f"Skipped: {skipped_categories_counter}")

        existing_activities = self.ghostfolio_api.get_all_activities_for_account(
            account_id
        )
        diff = get_diff(existing_activities, activities)
        if write_debug_files:
            logger.warn("Flag: WRITE_DEBUG_FILES is set, writing files")
            with open('deb_existing_activities.json', 'w') as outfile:
                logger.warn("Flag: writing existing_activities")
                json.dump(existing_activities, outfile)
            with open('deb_new_activities.json', 'w') as outfile:
                logger.warn("Flag: writing new activities")
                json.dump(activities, outfile)
            with open('deb_diff_activities.json', 'w') as outfile:
                logger.warn("Flag: writing new activities differences")
                json.dump(diff, outfile)

        if len(diff) == 0:
            logger.info("Nothing new to sync")
        else:
            self.ghostfolio_api.import_activities(diff)

    def set_cash_to_account(self, account_id, cash):
        if cash == 0:
            logger.info("No cash set, no cash retrieved")
            return False
        account = {
            "accountType": "SECURITIES",
            "balance": float(cash),
            "id": account_id,
            "currency": self.ghost_currency,
            "isExcluded": False,
            "name": "IBKR",
            "platformId": self.IBKRCATEGORY
        }

        self.ghostfolio_api.update_account(account_id, account)

    def delete_all_activities(self):
        self.ghostfolio_api.delete_all_activities()
