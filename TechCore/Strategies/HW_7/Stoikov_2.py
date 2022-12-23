from typing import List, Tuple, Union, Dict
import numpy as np
import pandas as pd

import TechCore
import TechCore.SimulatorVolume.utils
from TechCore.SimulatorVolume.simulator import MdUpdate, Order, OwnTrade, Sim
from TechCore.SimulatorVolume.utils import update_best_positions

# cheat
from TechCore.SimulatorVolume.simulator import Sim
from TechCore.SimulatorVolume.load_data import load_md_from_file

from collections import deque
import bisect


class Strategy:
    """
        Strategy that places midprice = future_price with Stoikov's spread

        This strategy places ask and bid order every `T` nanoseconds.
        If the order has not been executed within `T` nanoseconds, it is canceled.
    """
    def __init__(self, delay: float, risk_koef, time_oi, avg_sum_oi, avg_time_oi, avg_volatility, min_asset_value,
                 volatility_record_cooldown, volatility_horizon, order_intensity_min_samples, future_timestamp,
                 order_fees=-0.00004) -> None:

        """
        :param delay:                           Both "delay between orders" and "order hold" time
        :param risk_koef:                       From Stoikov article
        :param time_oi:                         Time in which we record average order intensity (oi)
        :param avg_sum_oi:                      Average sum of order sizes in window
        :param avg_time_oi:                     Average time of window ( -> time_oi)
        :param avg_volatility:                  Average volatility
        :param min_asset_value:                 Min asset value to scale our position (Stoikov article)
        :param volatility_record_cooldown:      We record best_ask for volatility once in ... seconds
        :param volatility_horizon:              How much records we use for volatility to be computed
        :param future_timestamp:                With how much ns from now we want to get future price
        :param order_intensity_min_samples:     Min samples to compute the order intensity
        :param order_fees:                      Market Making negative fees
        """

        self.future_timestamp = future_timestamp
        self.cheat_time_logs = None
        self.cheat_midprice_logs = None
        self.delay = delay

        self.risk_koef = risk_koef
        self.normalizer = min_asset_value

        self.time_oi = time_oi

        self.avg_sum_oi = avg_sum_oi

        self.avg_volatility = avg_volatility
        self.order_fees = order_fees
        self.avg_time_oi = avg_time_oi
        self.volatility_record_cooldown = volatility_record_cooldown
        self.volatility_horizon = volatility_horizon

        self.asset_position = 0
        self.usd_position = 0

        self.volatility_price_records = deque()
        self.volatility_time_records = deque()

        self.order_intensity_time_records = deque()
        self.order_intensity_size_records = deque()

        self.order_intensity_min_samples = order_intensity_min_samples

        self.pnl = 0
        self.midprice = 0
        self.total_liq = 0

        self.K = np.linspace(-0.3, 0.3, 13, endpoint=True)
        self.I = np.linspace(0, 1, 10, endpoint=False)
        self.S = np.linspace(0, 2, 20, endpoint=False)

        self.n = self.I.size
        self.m = self.S.size
        self.nm = self.n * self.m
        self.k = self.K.size

        self.Q_total = np.zeros(shape=(self.nm, self.nm))
        self.Q_success = self.Q_total.copy()
        self.R_total = np.zeros(shape=(self.nm, self.k))
        self.R_success = self.R_total.copy()
        self.T_total = np.zeros(shape=(self.nm, self.nm))
        self.T_success = self.T_total.copy()

        self.Q = None
        self.R = None
        self.T = None

        self.volatility = None
        self.scaled_order_intensity = None

        self.logs = {
            # Own Trade Logs
            'asset_position': [],       # current asset we hold

            'usd_position': [],         # current usd we hold
            'total_liq': [],            # total liquidity provided (in USD) - sum of all executed market making orders
            'pnl': [],                  # pnl = usd_position + asset_position * midprice
            'pnl_with_liq': [],         # pnl with negative commission for liquidity provided

            'own_trade_time': [],       # time records for own trades (for future plots)

            # Place Order Logs
            'best_ask': [],             # best ask price
            'best_bid': [],             # best bid price
            'midprice': [],             # (best_ask + best_bid) / 2
            'stock_spread': [],         # best_ask - best_bid = bid-ask-spread

            'ask_place': [],            # our price for ask order = indiff_price + my_spread/2
            'bid_place': [],            # our price for bid order = indiff_price - my_spread/2
            'indiff_price': [],         # "our" midprice from Stoikov's article
            'micro_price_adjustment': [],
            'my_spread': [],            # ask_place - bid_place = deltax2

            'ask_diff': [],             # ask_place - ask_price
            'bid_diff': [],             # bid_place - bid_price

            'order_intensity': [],      # intensity of trading (order execution)
            'volatility': [],           # market volatility
            'oi_window_size': [],       # size of order intensity window (in ns)

            'place_order_time': [],     # time records for place orders (for future plots)
        }

        self.cheat_logs = {
            'I': [],
            'S': [],
            'M': []
        }
        self.df_cheat_logs = None
        self.cheat_activ()
        self.estimate_transition_probabilities()

    def get_X_i(self, I, S):
        I_i = max(bisect.bisect_right(self.I, I) - 1, 0)
        S_i = max(bisect.bisect_right(self.S, S) - 1, 0)
        X_i = I_i * self.S.size + S_i
        return X_i

    def estimate_transition_probabilities(self):
        self.df_cheat_logs = pd.DataFrame(self.cheat_logs)
        self.df_cheat_logs['I_next'] = self.df_cheat_logs['I'].shift(-1)
        self.df_cheat_logs['S_next'] = self.df_cheat_logs['S'].shift(-1)
        self.df_cheat_logs['M_next'] = self.df_cheat_logs['M'].shift(-1)
        self.df_cheat_logs = self.df_cheat_logs.dropna()

        for I, S, M, I_next, S_next, M_next in zip(self.df_cheat_logs['I'], self.df_cheat_logs['S'],
                                                   self.df_cheat_logs['M'], self.df_cheat_logs['I_next'],
                                                   self.df_cheat_logs['S_next'], self.df_cheat_logs['M_next']):
            X_i = self.get_X_i(I, S)
            X_i_next = self.get_X_i(I_next, S_next)

            dM = M_next - M
            K_i = max(bisect.bisect_right(self.K, dM) - 1, 0)

            self.R_total[X_i] += 1
            self.R_success[X_i][K_i] += 1

            self.Q_total[X_i] += 1
            self.T_total[X_i] += 1
            if M_next == M:
                self.Q_success[X_i][X_i_next] += 1
            else:
                self.T_success[X_i][X_i_next] += 1

        self.Q = self.Q_success / self.Q_total
        self.Q = pd.DataFrame(self.Q).bfill().ffill().to_numpy()

        self.R = self.R_success / self.R_total
        self.R = pd.DataFrame(self.R).bfill().ffill().to_numpy()

        self.T = self.T_success / self.T_total
        self.T = pd.DataFrame(self.T).bfill().ffill().to_numpy()

    def cheat_activ(self):
        path_to_file = '../TechCore/data/md/btcusdt_Binance_LinearPerpetual/'
        run_time = pd.Timedelta(20, 'm').delta
        md = load_md_from_file(path=path_to_file, run_time=run_time)
        sim = Sim(md, 0, 0)

        best_ask = None
        best_bid = None

        ask_quantity = 1
        bid_quantity = 1

        self.cheat_midprice_logs = []
        self.cheat_time_logs = []

        while True:
            # get update from simulator
            receive_ts, updates = sim.tick()
            if updates is None:
                break

            for update in updates:
                if isinstance(update, TechCore.SimulatorVolume.utils.MdUpdate):
                    best_bid, best_ask, ask_quantity, bid_quantity =\
                        update_best_positions(best_bid, best_ask, ask_quantity, bid_quantity, update)

                    self.cheat_logs['I'].append(bid_quantity / (bid_quantity + ask_quantity))
                    self.cheat_logs['S'].append((best_ask - best_bid) / 2)
                    self.cheat_logs['M'].append((best_ask + best_bid) / 2)

                    self.cheat_time_logs.append(update.receive_ts)
                    self.cheat_midprice_logs.append((best_ask + best_bid) / 2)

    def get_future_price(self, receive_ts):
        ind = bisect.bisect_left(self.cheat_time_logs, receive_ts + self.future_timestamp)
        return self.cheat_midprice_logs[ind]

    def update_volatility(self, best_ask, receive_ts):
        prev_time = self.volatility_time_records[-1] if len(self.volatility_time_records) != 0 else 0
        if receive_ts - prev_time > self.volatility_record_cooldown:
            self.volatility_time_records.append(receive_ts)
            self.volatility_price_records.append(best_ask)

        while len(self.volatility_time_records) > self.volatility_horizon:
            self.volatility_time_records.popleft()
            self.volatility_price_records.popleft()

        self.volatility = np.array(self.volatility_price_records).std()**2 / self.avg_volatility

    def update_order_intensity(self):
        if len(self.order_intensity_time_records) > self.order_intensity_min_samples:
            while self.order_intensity_time_records[-1] - self.order_intensity_time_records[0] > self.time_oi:
                self.order_intensity_time_records.popleft()
                self.order_intensity_size_records.popleft()
            self.logs['oi_window_size'].append(self.order_intensity_time_records[-1] -
                                               self.order_intensity_time_records[0])
            total_time = self.order_intensity_time_records[-1] - self.order_intensity_time_records[0]
            total_sum = np.array(self.order_intensity_size_records).sum()
            scaled_sum = total_sum / self.avg_sum_oi
            scaled_time = total_time / self.avg_time_oi
            self.scaled_order_intensity = scaled_sum / scaled_time

    def run(self, sim: Sim) -> \
            Tuple[List[OwnTrade], List[MdUpdate], List[Union[OwnTrade, MdUpdate]], List[Order]]:
        """
            This function runs simulation

            Args:
                sim(Sim): simulator
            Returns:
                trades_list(List[OwnTrade]): list of our executed trades
                md_list(List[MdUpdate]): list of market data received by strategy
                updates_list( List[ Union[OwnTrade, MdUpdate] ] ): list of all updates
                received by strategy(market data and information about executed trades)
                all_orders(List[Order]): list of all placed orders
        """
        # market data list
        md_list: List[MdUpdate] = []
        # executed trades list
        trades_list: List[OwnTrade] = []
        # all updates list
        updates_list = []
        # current best positions
        best_bid = -np.inf
        best_ask = np.inf

        ask_quantity = 1
        bid_quantity = 1

        # last order timestamp
        prev_time = -np.inf
        # orders that have not been executed/canceled yet
        ongoing_orders: Dict[int, Order] = {}
        all_orders = []
        while True:
            # get update from simulator
            receive_ts, updates = sim.tick()
            if updates is None:
                break
            # save updates
            updates_list += updates
            for update in updates:
                # update best position

                if isinstance(update, TechCore.SimulatorVolume.utils.MdUpdate):
                    best_bid, best_ask, ask_quantity, bid_quantity =\
                        update_best_positions(best_bid, best_ask, ask_quantity, bid_quantity, update)

                    self.midprice = (best_ask + best_bid) / 2

                    self.volatility_price_records.append(self.midprice)

                    # скорее всего стоило exchange_ts, но его нет + разница слишком мала
                    self.volatility_time_records.append(update.receive_ts)

                    if update.trade is not None:
                        self.order_intensity_time_records.append(update.trade.receive_ts)
                        self.order_intensity_size_records.append(update.trade.size)

                    md_list.append(update)
                elif isinstance(update, TechCore.SimulatorVolume.utils.OwnTrade):
                    self.order_intensity_time_records.append(update.receive_ts)
                    self.order_intensity_size_records.append(update.size)

                    trades_list.append(update)
                    # delete executed trades from the dict
                    if update.order_id in ongoing_orders.keys():
                        ongoing_orders.pop(update.order_id)

                    if update.side == 'ASK':
                        self.asset_position -= update.size
                        self.usd_position += update.size * update.price
                    else:
                        self.asset_position += update.size
                        self.usd_position -= update.size * update.price

                    self.total_liq += update.size * update.price
                    self.pnl = self.asset_position * self.midprice + self.usd_position

                    self.logs['pnl'].append(self.pnl)
                    self.logs['asset_position'].append(self.asset_position)
                    self.logs['usd_position'].append(self.usd_position)
                    self.logs['total_liq'].append(self.total_liq)
                    self.logs['pnl_with_liq'].append(self.pnl - self.total_liq*self.order_fees)
                    self.logs['own_trade_time'].append(receive_ts)
                else:
                    assert False, 'Invalid type'

            if receive_ts - prev_time >= self.delay:
                prev_time = receive_ts
                midprice = (best_bid + best_ask) / 2

                self.update_volatility(
                    best_ask=best_ask,
                    receive_ts=receive_ts
                )
                self.update_order_intensity()

                if self.volatility is not None and self.scaled_order_intensity is not None:
                    self.logs['volatility'].append(self.volatility)
                    self.logs['order_intensity'].append(self.scaled_order_intensity)

                    # (T - t) = 1
                    Qi = np.linalg.inv(1 - self.Q)
                    G = Qi @ self.R @ self.K.T
                    B = Qi @ self.T

                    product = np.eye(self.nm, self.nm)
                    micro_price_adjustment = np.zeros(shape=(self.nm, ))
                    for i in range(20):
                        micro_price_adjustment += product @ G
                        product = product @ B

                    I = bid_quantity / (bid_quantity + ask_quantity)
                    S = (best_ask - best_bid) / 2
                    X_i = self.get_X_i(I, S)

                    indifference_price = midprice + micro_price_adjustment[X_i]
                    self.logs['indiff_price'].append(indifference_price)
                    self.logs['micro_price_adjustment'].append(micro_price_adjustment[X_i])
                    my_spread = self.risk_koef*self.volatility + 2/self.risk_koef*np.log(1 + self.risk_koef /
                                                                                         self.scaled_order_intensity)

                    ask_place = indifference_price + my_spread / 2
                    bid_place = indifference_price - my_spread / 2

                    self.logs['my_spread'].append(my_spread)
                    self.logs['ask_place'].append(ask_place)
                    self.logs['bid_place'].append(bid_place)
                    self.logs['ask_diff'].append(ask_place - best_ask)
                    self.logs['bid_diff'].append(bid_place - best_bid)
                    self.logs['midprice'].append(midprice)
                    self.logs['best_ask'].append(best_ask)
                    self.logs['best_bid'].append(best_bid)
                    self.logs['stock_spread'].append(best_ask - best_bid)
                    self.logs['place_order_time'].append(receive_ts)

                    # place order
                    bid_order = sim.place_order(receive_ts, 0.001, 'BID', bid_place)
                    ask_order = sim.place_order(receive_ts, 0.001, 'ASK', ask_place)
                    ongoing_orders[bid_order.order_id] = bid_order
                    ongoing_orders[ask_order.order_id] = ask_order

                    all_orders += [bid_order, ask_order]

            to_cancel = []
            for ID, order in ongoing_orders.items():
                if order.place_ts < receive_ts - self.delay:
                    sim.cancel_order(receive_ts, ID)
                    to_cancel.append(ID)
            for ID in to_cancel:
                ongoing_orders.pop(ID)

        return trades_list, md_list, updates_list, all_orders
