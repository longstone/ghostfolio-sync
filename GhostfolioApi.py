import json
from collections import namedtuple

import requests
import sys

from diskcache import Cache

import LoggerFactory
from EnvironmentConfiguration import EnvironmentConfiguration

SymbolLookupOverride = namedtuple('SymbolLookupOverride',
                                  'exists data_source symbol currency')
GhostfolioConfig = namedtuple('GhostfolioConfig',
                              'token host currency account_name, '
                              'platform_id platform_name')
GhostfolioTicker = namedtuple('GhostfolioTicker',
                              'data_source, symbol, currency')
GhostfolioImportActivity = namedtuple('GhostfolioImportActivity',
                                      'currency, dataSource, date, fee, quantity, '
                                      'symbol, type, unitPrice, accountId, comment')

DATA_SOURCE_YAHOO = "YAHOO"
cache = Cache(
    directory=EnvironmentConfiguration().file_write_location() + '.cache/ghostfolio-api')
logger = LoggerFactory.logger


class GhostfolioApi:

    def __init__(self, config: GhostfolioConfig):
        self.ghost_token = config.token
        self.ghost_host = config.host
        self.ghost_currency = config.currency
        self.ghost_account_sync_name = config.account_name
        self.ibkr_platform_name = config.platform_name
        self.account_name = config.account_name
        # todo should not do magic in ctor...
        if config.platform_id is None:
            self.ibkr_platform_id = self.__get_ibkr_platform_id()
        else:
            self.ibkr_platform_id = config.platform_id

    def update_account(self, account_id, account):
        url = f"{self.ghost_host}/api/v1/account/{account_id}"

        payload = json.dumps(account)
        headers = {
            'Authorization': f"Bearer {self.ghost_token}",
            'Content-Type': 'application/json'
        }
        try:
            self.__log_request(url, account)
            response = requests.request("PUT", url, headers=headers, data=payload)
        except Exception as e:
            self.__log_request_error(url, f"{e}")
            return False
        if response.status_code == 200:
            self.__log_request(url, f"Updated Cash for account {response.json()['id']}")
        else:
            self.__log_request_error(url, f"Failed create: {response.text}")
        return response.status_code == 200

    def delete_activity(self, act_id):
        url = f"{self.ghost_host}/api/v1/order/{act_id}"

        payload = {}
        headers = self.__get_header_with_ghostfolio_auth()
        try:
            self.__log_request(url)
            response = requests.request("DELETE", url, headers=headers, data=payload)
        except Exception as e:
            self.__log_request_error(url, e.__str__())
            return False

        return response.status_code == 200

    @staticmethod
    def validate_and_convert_response_to_assets(response):
        if response.status_code == 200:
            items = response.json().get('items')
            if len(items) > 1:
                # inform fuzzy match, but still do it :D
                logger.info("fuzzy match to first symbol for %s in %s",
                            response.request.url,
                            items)
            if len(items) >= 1:
                return True, items[0]
            return False, None
        else:
            raise Exception(response)

    def get_presenter_view_activated(self) -> bool:
        url = f"{self.ghost_host}/api/v1/user"
        headers = self.__get_header_with_ghostfolio_auth()
        try:
            self.__log_request(url)
            response = requests.request("GET", url, headers=headers)
            ghostfolio_account_settings = response.json()['settings']
            return 'isRestrictedView' in ghostfolio_account_settings
        except Exception as e:
            logger.warning(
                f"get_all_activities {url} error while fetching all activities: {e}"
            )
            raise e

    def set_presenterview(self, enabled):
        url = f"{self.ghost_host}/api/v1/user/setting"

        payload = '{"isRestrictedView":'
        payload += "true" if enabled else "false"
        payload += ' }'
        headers = self.__get_header_with_ghostfolio_auth()
        try:
            self.__log_request(url)
            response = requests.put(url, headers=headers, data=payload)
            return response.status_code == 200
        except Exception as e:
            logger.warning(
                f"set_presenterview {url} error while changing presenterview: {e}"
            )
            return False

    def get_all_activities(self) -> list[GhostfolioImportActivity]:
        presenter_view_initial_active = self.get_presenter_view_activated()
        if presenter_view_initial_active:
            logger.warning("presenterview active, not syncing")
            raise AssertionError("Presenterview is active, not syncing. "
                                 "Please deactivate Presenterview!")

        url = f"{self.ghost_host}/api/v1/order"

        payload = {}
        headers = self.__get_header_with_ghostfolio_auth()
        try:
            self.__log_request(url)
            response = requests.request("GET", url, headers=headers, data=payload)
        except Exception as e:
            logger.warning(
                f"get_all_activities {url} error while fetching all activities: {e}"
            )
            return []

        if response.status_code == 200:
            activities = response.json()['activities']
            self.__log_request(url, f"received {len(activities)} activities")
            import_activities_mapped: list[GhostfolioImportActivity] = []
            for activity in activities:
                import_activities_mapped.append(
                    self.map_activity_to_import_activity(activity))
            return import_activities_mapped
        else:
            return []

    def import_activities(self, bulk: list[GhostfolioImportActivity]):
        chunks = self.__generate_chunks(bulk, 10)
        for acts in chunks:
            url = f"{self.ghost_host}/api/v1/import"
            sorted_acts: list[GhostfolioImportActivity] \
                = sorted(acts, key=lambda x: x.date)
            acts_as_dicts = []
            for act in sorted_acts:
                acts_as_dicts.append(act._asdict())
            formatted_acts = json.dumps(
                {"activities": acts_as_dicts}
            )
            payload = formatted_acts
            headers = {
                'Authorization': f"Bearer {self.ghost_token}",
                'Content-Type': 'application/json'
            }
            logger.debug("import_activities Adding activities: \n" + formatted_acts)
            try:
                self.__log_request(url, f"adding {len(acts_as_dicts)} activities")
                response = requests.request("POST", url, headers=headers, data=payload)
            except Exception as e:
                self.__log_request_error(
                    url,
                    f"with payload: {payload} failed with {e}"
                )
                return False
            if response.status_code == 201:
                logger.info(
                    f"import_activities {url} created {len(acts_as_dicts)} activities"
                )
            else:
                message = f"Failed create following activities:" \
                          f" {acts_as_dicts}: {response.text}"
                self.__log_request_error(url, message)
            if response.status_code != 201:
                return False
        return True

    def add_activity(self, act):
        url = f"{self.ghost_host}/api/v1/order"

        payload = json.dumps(act)
        headers = {
            'Authorization': f"Bearer {self.ghost_token}",
            'Content-Type': 'application/json'
        }
        logger.info("Adding activity: " + json.dumps(act))
        try:
            response = requests.request("POST", url, headers=headers, data=payload)
        except Exception as e:
            logger.error(e)
            return False
        if response.status_code == 201:
            self.__log_request(url, f"created {response.json()['id']}")
        else:
            self.__log_request_error(url, f"Failed create: {response.text}")
        return response.status_code == 201

    def create_or_get_ibkr_account(self):
        accounts = self.get_ghostfolio_accounts()
        for account in accounts:
            if account["name"] == self.ghost_account_sync_name:
                return account
        return self.__create_ibkr_account()

    def __create_ibkr_account(self):
        account = {
            "accountType": "SECURITIES",
            "balance": 0,
            "currency": self.ghost_currency,
            "isExcluded": False,
            "name": self.ghost_account_sync_name,
            "platformId": self.ibkr_platform_id,
        }
        return self.create_account(account)

    def delete_all_activities(self, account_id):
        acts: list[GhostfolioImportActivity] = self.get_all_activities_for_account(
            account_id
        )

        if not acts:
            logger.info("No activities to delete")
            return True
        complete = True

        for act in acts:
            if act.accountId == account_id:
                act_complete = self.delete_activity(act.accountId)
                complete = complete and act_complete
                if act_complete:
                    logger.info("Deleted: %s", act.accountId)
                else:
                    logger.warning("Failed Delete: %s", act.accountId)
        return complete

    def get_all_activities_for_account(
            self,
            account_id: str
    ) -> list[GhostfolioImportActivity]:
        acts: list[GhostfolioImportActivity] = self.get_all_activities()
        filtered_acts: list[GhostfolioImportActivity] = []
        for act in acts:
            if act.accountId == account_id:
                filtered_acts.append(act)
        return filtered_acts

    @staticmethod
    def map_activity_to_import_activity(act) -> GhostfolioImportActivity:
        symbol_profile = act['SymbolProfile']
        import_activity = GhostfolioImportActivity(
            symbol_profile['currency'],
            symbol_profile['dataSource'],
            act['date'],
            act['fee'],
            act['quantity'],
            symbol_profile['symbol'],
            act['type'],
            act['unitPrice'],
            act['accountId'],
            act.get('comment'),
        )
        return import_activity

    def create_account(self, account):
        url = f"{self.ghost_host}/api/v1/account"

        payload = json.dumps(account)
        headers = {
            'Authorization': f"Bearer {self.ghost_token}",
            'Content-Type': 'application/json'
        }
        try:
            response = requests.request("POST", url, headers=headers, data=payload)
        except Exception as e:
            print(e)
            return ""
        if response.status_code == 201:
            return response.json()["id"]
        logger.warning(f"create_account: Failed creating {url}: {account}")
        return ""

    @cache.memoize(tag='get_ghostfolio_accounts', expire=600)
    def get_ghostfolio_accounts(self):
        url = f"{self.ghost_host}/api/v1/account"

        payload = {}
        headers = self.__get_header_with_ghostfolio_auth()
        try:
            self.__log_request(url)
            response = requests.request("GET", url, headers=headers, data=payload)
        except Exception as e:
            logger.error(e)
            return []
        if response.status_code == 200:
            return response.json()['accounts']
        else:
            if response.status_code == 401:
                logger.error("Token (GHOST_TOKEN) Expired! "
                             "see "
                             "https://github.com/longstone/ghostfolio-sync?tab=readme"
                             "-ov-file#ghostfolio"
                             "renew token")
            raise Exception(response)

    def get_ticker(self, isin, symbol) -> GhostfolioTicker:
        override: SymbolLookupOverride = self.__lookup_overrides(isin, symbol)
        if override.exists:
            return GhostfolioTicker(
                override.data_source,
                override.symbol,
                override.currency
            )
        # for now only yahoo
        data_source = DATA_SOURCE_YAHOO
        successful, ticker = self.__lookup_asset(isin)
        if not successful:
            successful, ticker = self.__lookup_asset(symbol)
            if not successful:
                raise Exception(f"no symbol found for {isin} {symbol}")
        return GhostfolioTicker(
            data_source,
            ticker.get('symbol'),
            ticker.get('currency')
        )

    @cache.memoize(tag='__lookup_asset')
    def __lookup_asset(self, query):
        url = f"{self.ghost_host}/api/v1/symbol/lookup?query={query}"
        headers = self.__get_header_with_ghostfolio_auth()
        try:
            self.__log_request(url)
            response = requests.request("GET", url, headers=headers)
            return self.validate_and_convert_response_to_assets(response)
        except Exception as e:
            self.__log_request_error(url, f"lookup asset: {query} failed with {e}")
            return False, None

    @staticmethod
    def __generate_chunks(lst: list[GhostfolioImportActivity], n):
        for i in range(0, len(lst), n):
            yield lst[i:i + n]

    def __get_header_with_ghostfolio_auth(self):
        return {
            'Authorization': f"Bearer {self.ghost_token}",
        }

    def __log_request(self, url, message="no-message"):
        previous_function_name = sys._getframe(1).f_code.co_name
        logger.debug(f"{previous_function_name} {url}: {message}")

    def __log_request_error(self, url, message="no-message"):
        previous_function_name = sys._getframe(1).f_code.co_name
        logger.error(f"{previous_function_name} {url}: {message}")

    def __lookup_overrides(self, isin, symbol) -> SymbolLookupOverride:
        # TODO create a way to lookup stuff
        if isin == 'DE000A3MQQ17':
            return SymbolLookupOverride(True, DATA_SOURCE_YAHOO, 'FRE.DE', 'EUR')
        if isin == 'NL0015001L59':
            return SymbolLookupOverride(True, DATA_SOURCE_YAHOO, 'SHEL.L', 'GBp')
        if isin == 'US09075V1026':
            return SymbolLookupOverride(True, DATA_SOURCE_YAHOO, 'BNTX', 'USD')
        if isin == 'DE000A40UTE1':
            return SymbolLookupOverride(True, DATA_SOURCE_YAHOO, 'AR40.HM', 'EUR')
        return SymbolLookupOverride(False, None, None, None)

    def __get_ibkr_platform_id(self):
        url = f"{self.ghost_host}/api/v1/info"
        headers = self.__get_header_with_ghostfolio_auth()
        try:
            self.__log_request(url)
            response = requests.request("GET", url, headers=headers)
            for platform in response.json()['platforms']:
                if platform['name'] == self.ibkr_platform_name:
                    return platform['id']
            raise Exception(f"no platform found for name {self.ibkr_platform_name} "
                            f"in {response.json()['platforms']}")
        except Exception as e:
            self.__log_request_error(url, f"lookup failed with {e}")
            return None

    def get_dividends_to_import(self, account_id, isin):
        ticker = self.get_ticker(isin, None)
        import_list = list(filter(lambda x: x.accountId == account_id,
                                  self.__get_dividends_to_import(ticker)))
        if import_list:
            return import_list
        return None

    def __get_dividends_to_import(self, ticker) -> list[GhostfolioImportActivity]:
        url = f"{self.ghost_host}/api/v1/" \
              f"import/dividends/{ticker.data_source}/{ticker.symbol}"
        headers = self.__get_header_with_ghostfolio_auth()
        try:
            self.__log_request(url)
            response = requests.request("GET", url, headers=headers)
            activities_existing = list(response.json().get('activities'))
            activities_to_import = list(filter(lambda x: x.get('error') is None,
                                               activities_existing))
            return list(map(lambda x: self.map_activity_to_import_activity(x),
                            activities_to_import))
        except Exception as e:
            self.__log_request_error(url, f"lookup dividens: failed with {e}")
