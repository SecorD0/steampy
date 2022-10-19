import decimal

import bs4
import urllib.parse as urlparse
from typing import List, Union

import json
import requests
from pretty_utils.type_functions.strings import text_between

from steampy import guard
from steampy.chat import SteamChat
from steampy.confirmation import ConfirmationExecutor
from steampy.exceptions import SevenDaysHoldException, LoginRequired, ApiException, InvalidProxy, UnsuccLogout
from steampy.login import LoginExecutor, InvalidCredentials
from steampy.market import SteamMarket
from steampy.models import Asset, TradeOfferState, SteamUrl, GameOptions
from steampy.utils import texts_between, merge_items_with_descriptions_from_inventory, steam_id_to_account_id, \
    merge_items_with_descriptions_from_offers, get_description_key, merge_items_with_descriptions_from_offer, \
    account_id_to_steam_id, get_key_value_from_url, parse_price


def login_required(func):
    def func_wrapper(self, *args, **kwargs):
        if not self.was_login_executed:
            raise LoginRequired('Use login method first')
        else:
            return func(self, *args, **kwargs)

    return func_wrapper


class SteamClient:
    def __init__(self, api_key: str = None, username: str = None, password: str = None, steam_guard: str = None,
                 proxy: str = None, check_proxy: bool = True) -> None:
        self._api_key = api_key
        self.proxy = proxy
        self._session = requests.Session()
        self.steam_guard = steam_guard
        self.was_login_executed = False
        self.username = username
        self._password = password
        self.check_proxy = check_proxy

        if self.proxy:
            proxies = {'http': self.proxy, 'https': self.proxy}
            self._session.proxies.update(proxies)

        self.market = SteamMarket(self._session)
        self.chat = SteamChat(self._session)

    def login(self, username: str, password: str, steam_guard: str) -> None:
        self.steam_guard = guard.load_steam_guard(steam_guard)
        self.username = username
        self._password = password
        LoginExecutor(username, password, self.steam_guard['shared_secret'], self._session).login()
        self.was_login_executed = True
        self.market._set_login_executed(self.steam_guard, self._get_session_id())
        self.PROFILE_URL = f"https://steamcommunity.com/profiles/{self.steam_guard['Session']['SteamID']}"

    @login_required
    def logout(self) -> None:
        url = SteamUrl.STORE_URL + '/logout/'
        data = {'sessionid': self._get_session_id()}
        self._session.post(url, data=data)
        if self.is_session_alive():
            raise UnsuccLogout("Logout unsuccessful")
        self.was_login_executed = False

    def __enter__(self):
        if None in [self.username, self._password, self.steam_guard]:
            raise InvalidCredentials('You have to pass username, password and steam_guard'
                                     'parameters when using "with" statement')

        if self.proxy and self.check_proxy:
            response = self._session.get('https://whoer.net/')
            if '@' in self.proxy:
                proxy = text_between(self.proxy, '@', ':')
            else:
                proxy = text_between(self.proxy, end=':')

            if proxy not in response.text:
                soup = bs4.BeautifulSoup(response.text, "html.parser")
                your_ip = soup.find('strong', class_='your-ip').get_text(strip=True)
                raise InvalidProxy(f"Proxy doesn't work! Your IP is {your_ip}")

        self.login(self.username, self._password, self.steam_guard)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.logout()

    @login_required
    def is_session_alive(self):
        steam_login = self.username
        main_page_response = self._session.get(SteamUrl.COMMUNITY_URL)
        return steam_login.lower() in main_page_response.text.lower()

    def api_call(self, request_method: str, interface: str, api_method: str, version: str,
                 params: dict = None) -> requests.Response:
        url = '/'.join([SteamUrl.API_URL, interface, api_method, version])
        if request_method == 'GET':
            response = requests.get(url, params=params)
        else:
            response = requests.post(url, data=params)
        if self.is_invalid_api_key(response):
            raise InvalidCredentials('Invalid API key')
        return response

    @staticmethod
    def is_invalid_api_key(response: requests.Response) -> bool:
        msg = 'Access is denied. Retrying will not help. Please verify your <pre>key=</pre> parameter'
        return msg in response.text

    @login_required
    def get_my_inventory(self, game: GameOptions, merge: bool = True, count: int = 5000) -> dict:
        steam_id = str(self.steam_guard['Session']['SteamID'])
        return self.get_partner_inventory(steam_id, game, merge, count)

    @login_required
    def get_trade_url(self) -> str:
        return text_between(self._session.get(self.PROFILE_URL + "/tradeoffers/privacy#trade_offer_access_url").text,
                            'value="', '" readonly')

    @login_required
    def get_partner_inventory(self, partner_steam_id: str, game: GameOptions, merge: bool = True,
                              count: int = 5000) -> dict:
        url = '/'.join([SteamUrl.COMMUNITY_URL, 'inventory', partner_steam_id, game.app_id, game.context_id])
        params = {'l': 'english',
                  'count': count}
        response_dict = self._session.get(url, params=params).json()
        if response_dict['success'] != 1:
            raise ApiException('Success value should be 1.')
        if merge:
            return merge_items_with_descriptions_from_inventory(response_dict, game)
        return response_dict

    def _get_session_id(self) -> str:
        return self._session.cookies.get_dict()['sessionid']

    def get_trade_offers_summary(self) -> dict:
        params = {'key': self._api_key}
        return self.api_call('GET', 'IEconService', 'GetTradeOffersSummary', 'v1', params).json()

    def get_trade_offers(self, without_api_key: bool = False, merge: bool = True) -> dict:
        if without_api_key:
            url = self.PROFILE_URL + '/tradeoffers/sent/'
            response = self._session.post(url)
            response_soup = bs4.BeautifulSoup(response.text, "html.parser")
            response = response_soup.find_all('div', class_='tradeoffer')
            if merge:
                response_dict = {}
                for trade in response:
                    item = {}
                    trade_id = text_between(trade['id'], 'tradeofferid_')
                    status = 'Active'
                    try:
                        banner = trade.find('div', class_='tradeoffer_items_banner').get_text(strip=True)
                        if banner == 'Awaiting Mobile Confirmation':
                            status = 'Awaiting mobile confirmation'
                        elif 'Canceled' in banner:
                            status = 'Canceled'
                        elif 'Accepted' in banner:
                            status = 'Accepted'
                        elif banner == 'Items Now Unavailable For Trade':
                            status = 'Items unavailable'
                        else:
                            status = banner
                    except:
                        pass

                    items_from_me = []
                    for trade_item in trade.find('div', class_='tradeoffer_items primary').find_all(
                            class_='trade_item'):
                        items_from_me.append(trade_item['data-economy-item'])

                    items_from_them = []
                    for trade_item in trade.find('div', class_='tradeoffer_items secondary').find_all(
                            class_='trade_item'):
                        items_from_them.append(trade_item['data-economy-item'])

                    item['id'] = trade_id
                    item['status'] = status
                    item['items_from_me'] = items_from_me
                    item['items_from_them'] = items_from_them
                    response_dict[trade_id] = item

            else:
                response_dict = response

            return response_dict

        else:
            params = {'key': self._api_key,
                      'get_sent_offers': 1,
                      'get_received_offers': 1,
                      'get_descriptions': 1,
                      'language': 'english',
                      'active_only': 1,
                      'historical_only': 0,
                      'time_historical_cutoff': ''}
            response = self.api_call('GET', 'IEconService', 'GetTradeOffers', 'v1', params).json()
            response = self._filter_non_active_offers(response)
            if merge:
                response = merge_items_with_descriptions_from_offers(response)
            return response

    @staticmethod
    def _filter_non_active_offers(offers_response):
        offers_received = offers_response['response'].get('trade_offers_received', [])
        offers_sent = offers_response['response'].get('trade_offers_sent', [])
        offers_response['response']['trade_offers_received'] = list(
            filter(lambda offer: offer['trade_offer_state'] == TradeOfferState.Active, offers_received))
        offers_response['response']['trade_offers_sent'] = list(
            filter(lambda offer: offer['trade_offer_state'] == TradeOfferState.Active, offers_sent))
        return offers_response

    def get_trade_offer(self, trade_offer_id: str, merge: bool = True) -> dict:
        params = {'key': self._api_key,
                  'tradeofferid': trade_offer_id,
                  'language': 'english'}
        response = self.api_call('GET', 'IEconService', 'GetTradeOffer', 'v1', params).json()
        if merge and "descriptions" in response['response']:
            descriptions = {get_description_key(offer): offer for offer in response['response']['descriptions']}
            offer = response['response']['offer']
            response['response']['offer'] = merge_items_with_descriptions_from_offer(offer, descriptions)
        return response

    def get_trade_history(self,
                          max_trades=100,
                          start_after_time=None,
                          start_after_tradeid=None,
                          get_descriptions=True,
                          navigating_back=True,
                          include_failed=True,
                          include_total=True) -> dict:
        params = {
            'key': self._api_key,
            'max_trades': max_trades,
            'start_after_time': start_after_time,
            'start_after_tradeid': start_after_tradeid,
            'get_descriptions': get_descriptions,
            'navigating_back': navigating_back,
            'include_failed': include_failed,
            'include_total': include_total
        }
        response = self.api_call('GET', 'IEconService', 'GetTradeHistory', 'v1', params).json()
        return response

    @login_required
    def get_trade_receipt(self, trade_id: str) -> list:
        html = self._session.get("https://steamcommunity.com/trade/{}/receipt".format(trade_id)).content.decode()
        items = []
        for item in texts_between(html, "oItem = ", ";\r\n\toItem"):
            items.append(json.loads(item))
        return items

    @login_required
    def accept_trade_offer(self, trade_offer_id: str) -> dict:
        trade = self.get_trade_offer(trade_offer_id)
        trade_offer_state = TradeOfferState(trade['response']['offer']['trade_offer_state'])
        if trade_offer_state is not TradeOfferState.Active:
            raise ApiException("Invalid trade offer state: {} ({})".format(trade_offer_state.name,
                                                                           trade_offer_state.value))
        partner = self._fetch_trade_partner_id(trade_offer_id)
        session_id = self._get_session_id()
        accept_url = SteamUrl.COMMUNITY_URL + '/tradeoffer/' + trade_offer_id + '/accept'
        params = {'sessionid': session_id,
                  'tradeofferid': trade_offer_id,
                  'serverid': '1',
                  'partner': partner,
                  'captcha': ''}
        headers = {'Referer': self._get_trade_offer_url(trade_offer_id)}
        response = self._session.post(accept_url, data=params, headers=headers).json()
        if response.get('needs_mobile_confirmation', False):
            return self._confirm_transaction(trade_offer_id)
        return response

    def _fetch_trade_partner_id(self, trade_offer_id: str) -> str:
        url = self._get_trade_offer_url(trade_offer_id)
        offer_response_text = self._session.get(url).text
        if 'You have logged in from a new device. In order to protect the items' in offer_response_text:
            raise SevenDaysHoldException("Account has logged in a new device and can't trade for 7 days")
        return text_between(offer_response_text, "var g_ulTradePartnerSteamID = '", "';")

    def _confirm_transaction(self, trade_offer_id: str) -> dict:
        confirmation_executor = ConfirmationExecutor(self.steam_guard['identity_secret'],
                                                     self.steam_guard['Session']['SteamID'], self._session)
        return confirmation_executor.send_trade_allow_request(trade_offer_id)

    def decline_trade_offer(self, trade_offer_id: str) -> dict:
        url = 'https://steamcommunity.com/tradeoffer/' + trade_offer_id + '/decline'
        response = self._session.post(url, data={'sessionid': self._get_session_id()}).json()
        return response

    def cancel_trade_offer(self, trade_offer_id: str) -> dict:
        url = 'https://steamcommunity.com/tradeoffer/' + trade_offer_id + '/cancel'
        response = self._session.post(url, data={'sessionid': self._get_session_id()}).json()
        return response

    @login_required
    def make_offer(self, items_from_me: List[Asset], items_from_them: List[Asset], partner_steam_id: str,
                   message: str = '') -> dict:
        offer = self._create_offer_dict(items_from_me, items_from_them)
        session_id = self._get_session_id()
        url = SteamUrl.COMMUNITY_URL + '/tradeoffer/new/send'
        server_id = 1
        params = {
            'sessionid': session_id,
            'serverid': server_id,
            'partner': partner_steam_id,
            'tradeoffermessage': message,
            'json_tradeoffer': json.dumps(offer),
            'captcha': '',
            'trade_offer_create_params': '{}'
        }
        partner_account_id = steam_id_to_account_id(partner_steam_id)
        headers = {'Referer': SteamUrl.COMMUNITY_URL + '/tradeoffer/new/?partner=' + partner_account_id,
                   'Origin': SteamUrl.COMMUNITY_URL}
        response = self._session.post(url, data=params, headers=headers).json()
        if response.get('needs_mobile_confirmation'):
            response.update(self._confirm_transaction(response['tradeofferid']))
        return response

    def get_profile(self, steam_id: str) -> dict:
        params = {'steamids': steam_id, 'key': self._api_key}
        response = self.api_call('GET', 'ISteamUser', 'GetPlayerSummaries', 'v0002', params)
        data = response.json()
        return data['response']['players'][0]

    def get_friend_list(self, steam_id: str, relationship_filter: str = "all") -> dict:
        params = {
            'key': self._api_key,
            'steamid': steam_id,
            'relationship': relationship_filter
        }
        resp = self.api_call("GET", "ISteamUser", "GetFriendList", "v1", params)
        data = resp.json()
        return data['friendslist']['friends']

    @staticmethod
    def _create_offer_dict(items_from_me: List[Asset], items_from_them: List[Asset]) -> dict:
        return {
            'newversion': True,
            'version': 5,
            'me': {
                'assets': [asset.to_dict() for asset in items_from_me],
                'currency': [],
                'ready': False
            },
            'them': {
                'assets': [asset.to_dict() for asset in items_from_them],
                'currency': [],
                'ready': False
            }
        }

    @login_required
    def get_escrow_duration(self, trade_offer_url: str) -> int:
        headers = {'Referer': SteamUrl.COMMUNITY_URL + urlparse.urlparse(trade_offer_url).path,
                   'Origin': SteamUrl.COMMUNITY_URL}
        response = self._session.get(trade_offer_url, headers=headers).text
        my_escrow_duration = int(text_between(response, "var g_daysMyEscrow = ", ";"))
        their_escrow_duration = int(text_between(response, "var g_daysTheirEscrow = ", ";"))
        return max(my_escrow_duration, their_escrow_duration)

    @login_required
    def make_offer_with_url(self, items_from_me: List[Asset], items_from_them: List[Asset],
                            trade_offer_url: str, message: str = '', case_sensitive: bool = True) -> dict:
        token = get_key_value_from_url(trade_offer_url, 'token', case_sensitive)
        partner_account_id = get_key_value_from_url(trade_offer_url, 'partner', case_sensitive)
        partner_steam_id = account_id_to_steam_id(partner_account_id)
        offer = self._create_offer_dict(items_from_me, items_from_them)
        session_id = self._get_session_id()
        url = SteamUrl.COMMUNITY_URL + '/tradeoffer/new/send'
        server_id = 1
        trade_offer_create_params = {'trade_offer_access_token': token}
        params = {
            'sessionid': session_id,
            'serverid': server_id,
            'partner': partner_steam_id,
            'tradeoffermessage': message,
            'json_tradeoffer': json.dumps(offer),
            'captcha': '',
            'trade_offer_create_params': json.dumps(trade_offer_create_params)
        }
        headers = {'Referer': SteamUrl.COMMUNITY_URL + urlparse.urlparse(trade_offer_url).path,
                   'Origin': SteamUrl.COMMUNITY_URL}
        response = self._session.post(url, data=params, headers=headers).json()
        if response.get('needs_mobile_confirmation'):
            response.update(self._confirm_transaction(response['tradeofferid']))
        return response

    @staticmethod
    def _get_trade_offer_url(trade_offer_id: str) -> str:
        return SteamUrl.COMMUNITY_URL + '/tradeoffer/' + trade_offer_id

    @login_required
    def get_wallet_balance(self, convert_to_decimal: bool = True) -> Union[str, decimal.Decimal]:
        url = SteamUrl.STORE_URL + '/account/history/'
        response = self._session.get(url)
        response_soup = bs4.BeautifulSoup(response.text, "html.parser")
        balance = response_soup.find(id='header_wallet_balance').string
        if convert_to_decimal:
            return parse_price(balance)
        else:
            return balance

    @login_required
    def get_game_bans(self) -> dict:
        url = 'https://help.steampowered.com/ru/wizard/VacBans'
        response = self._session.get(url)
        response_soup = bs4.BeautifulSoup(response.text, 'html.parser')
        bans = response_soup.find_all('div', class_='vac_ban_header')
        response_dict = {'VAC bans': None, 'Game bans': None}
        if bans:
            for ban in bans:
                if 'VAC' in ban.get_text(strip=True):
                    games = ban.parent.find('div', class_='refund_info_box').find_all('span',
                                                                                      class_='help_highlight_text')
                    games_list = []
                    for game in games:
                        games_list.append(game.get_text(strip=True))
                    response_dict['VAC bans'] = games_list
                else:
                    games = ban.parent.find('div', class_='refund_info_box').find_all('span',
                                                                                      class_='help_highlight_text')
                    games_list = []
                    for game in games:
                        games_list.append(game.get_text(strip=True))
                    response_dict['Game bans'] = games_list
        return response_dict

    @login_required
    def set_privacy_settings(self, my_profile: int = 3, game_details: int = 3, playtime: int = 3, friend_list: int = 3,
                             inventory: int = 3, gifts: int = 3, comments: int = 2) -> Union[str, decimal.Decimal]:
        params = (('sessionid', (None, self._get_session_id())),
                  ('Privacy', (None, json.dumps(
                      {'PrivacyProfile': my_profile, 'PrivacyInventory': inventory, 'PrivacyInventoryGifts': gifts,
                       'PrivacyOwnedGames': game_details, 'PrivacyPlaytime': playtime,
                       'PrivacyFriendsList': friend_list}))),
                  ('eCommentPermission', (None, comments)))
        headers = {'Referer': self.PROFILE_URL + '/edit/settings', 'Origin': SteamUrl.COMMUNITY_URL}
        response = self._session.post(self.PROFILE_URL + '/ajaxsetprivacy/', files=params, headers=headers).json()
        return response
