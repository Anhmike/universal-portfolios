from sklearn.covariance import EmpiricalCovariance
from sklearn.base import BaseEstimator
import numpy as np
import pandas as pd
from .. import tools
from sklearn.decomposition import PCA
from numpy.linalg import inv
from scipy.linalg import sqrtm
from sklearn import covariance
import logging
import plotly.graph_objs as go


# expenses + tax dividend
EXPENSES = {
    'CASH': 0.,
    'TMF': 0.0108,
    'DPST': 0.0104,
    'ASHR': 0.0065,
    'TQQQ': 0.0095,
    'UGLD': 0.0135,
    'ERX': 0.01,
    'RING': 0.0039,
    'LABU': 0.0109,
    'YINN': 0.013,
    'SOXL': 0.0097,
    'RETL': 0.0105,
    'TYD': 0.0097,
    'UDOW': 0.0095,
    'GBTC': 0.02,
    'FAS': 0.0096,
    'MCHI': 0.0064,
    'CQQQ': 0.0070,
    'CHIX': 0.0065,
    'UBT': 0.0095,
    'FXI': 0.0074,
    'DRN': 0.0109,
    'O': 0 + 0.045 * 0.15,
    'DSUM': 0.0045 + 0.035 * 0.15,
    'SPY': 0.0009,
    'TLT': 0.0015,
    'ZIV': 0.0135,
    'GLD': 0.004,
    'BABA': 0.,
    'BIDU': 0.,
    'IEF': 0.0015,
    'KWEB': 0.007,
    'JPNL': 0.0121,
    'EDC': 0.0148,
    'EEMV': 0.0025,
    'USMV': 0.0015,
    'ACWV': 0.002,
    'EFAV': 0.002,
    'KRE': 0.0035,
    'ZN': 0.,
    'RFR': 0.,
}


class SharpeEstimator(object):

    def __init__(self, global_sharpe=0.4, override_sharpe=None, override_mean=None, capm=None, rfr=0., verbose=False):
        """
        :param rfr: risk-free rate
        """
        self.override_sharpe = override_sharpe or {}
        self.override_mean = override_mean or {}
        self.capm = capm or {}
        self.global_sharpe = global_sharpe
        self.rfr = rfr
        self.verbose = verbose

    def fit(self, X, sigma):
        """
        formula for mean is:
            sh * vol + rf - expenses
        """
        est_sh = pd.Series(self.global_sharpe, index=sigma.index)
        for k, v in self.override_sharpe.items():
            if k in est_sh:
                est_sh[k] = v

        # assume that all assets have yearly sharpe ratio 0.5 and deduce return from volatility
        vol = pd.Series(np.sqrt(np.diag(sigma)), index=sigma.index)
        missing_expenses = set(sigma.index) - set(EXPENSES.keys())
        if missing_expenses:
            logging.warning('Missing ETF expense for {}'.format(missing_expenses))
        expenses = pd.Series([EXPENSES.get(c, 0.005) for c in sigma.index], index=sigma.index)
        mu = est_sh * vol + self.rfr - expenses

        # adjust CASH
        if 'CASH' in X.columns:
            mu['CASH'] = X.CASH[-1]**(tools.freq(X.index)) - 1

        for asset, market in self.capm.items():
            beta = sigma.loc[market, asset] / sigma.loc[market, market]
            prev_mu = mu[asset]
            mu[asset] = self.rfr + beta * (mu[market] - self.rfr)
            if self.verbose:
                print(f'Beta of {beta:.2f} changed {asset} mean return from {prev_mu:.1%} to {mu[asset]:.1%}')

        if self.override_mean:
            for k, v in self.override_mean.items():
                if k in mu.index:
                    mu.loc[k] = v

        if self.verbose:
            print(pd.DataFrame({
                'volatility': vol,
                'mean': mu,
            }))

        return mu


class MuVarianceEstimator(object):

    def fit(self, X, sigma):
        # assume that all assets have yearly sharpe ratio 1 and deduce return from volatility
        mu = np.matrix(sigma).dot(np.ones(sigma.shape[0]))
        return mu


class HistoricalEstimator(object):

    def __init__(self, window):
        self.window = window

    def fit(self, X, sigma):
        # assume that all assets have yearly sharpe ratio 1 and deduce return from volatility
        if self.window:
            X = X.iloc[-self.window:]

        mu = X.mean()
        mu = (1 + mu)**tools.freq(X.index) - 1
        return mu


class MixedEstimator(object):
    """ Combines historical estimation with sharpe estimation from volatility.
    Has two parameters alpha and beta that works like this:
    alpha in (0, 1) controls regularization of covariance matrix
        alpha = 0 -> assume covariance is zero
        alpha = 1 -> don't regularize
    beta in (0, inf) controls weight we give on historical mean
        beta = 0 -> return is proportional to volatility if alpha = 0 or row sums
            of covariance matrix if alpha = 1
        beta = inf -> use historical return
    """

    def __init__(self, window=None, alpha=0., beta=0.):
        self.GLOBAL_SHARPE = SharpeEstimator.GLOBAL_SHARPE
        self.historical_estimator = HistoricalEstimator(window=window)
        self.alpha = alpha
        self.beta = beta

    def fit(self, X, sigma):
        alpha = self.alpha
        beta = self.beta
        m = X.shape[1]

        # calculate historical return
        historical_mu = self.historical_estimator.fit(X, sigma)

        # regularize sigma
        reg_sigma = alpha * sigma + (1 - alpha) * np.diag(np.diag(sigma))

        # avoid computing inversions
        if beta == 0:
            mu = self.GLOBAL_SHARPE * np.real(sqrtm(reg_sigma)).dot(np.ones(m))
        else:
            # estimate mean
            mu_tmp = beta * historical_mu + self.GLOBAL_SHARPE * inv(np.real(sqrtm(reg_sigma))).dot(np.ones(m))
            mu = inv(inv(reg_sigma) + beta * np.eye(m)).dot(mu_tmp)

        return pd.Series(mu, index=X.columns)


class PCAEstimator(object):

    def __init__(self, window, n_components='mle'):
        self.window = window
        self.n_components = n_components

    def fit(self, X, sigma):
        # take recent period (PCA could be estimated from sigma too)
        R = X.iloc[-self.window:].fillna(0.)

        pca = PCA(n_components=self.n_components).fit(R)
        pca_mu = np.sqrt(pca.explained_variance_) * 0.5 * np.sqrt(tools.freq(X.index))
        comp = pca.components_.T

        # principal components have arbitraty orientation -> choose orientation to maximize final mean return
        comp = comp * np.sign(comp.sum(0))

        pca_mu = comp.dot(pca_mu)
        pca_mu = pd.Series(pca_mu, index=X.columns)
        return pca_mu


class MLEstimator(object):
    """ Predict mean using sklearn model. """

    def __init__(self, model, freq='M'):
        self.model = model
        self.freq = freq

    def featurize(self, H):
        X = pd.DataFrame({
            'last_sh': H.shift(1).stack(),
            'history_sh': pd.expanding_mean(H).shift(1).stack(),
            'history_sh_vol': pd.expanding_std(H).shift(1).stack(),
            'nr_days': H.notnull().cumsum().stack()
        })
        return X

    def fit(self, X, sigma):
        # work with sharpe ratio of log returns (assume raw returns)
        R = np.log(X + 1)
        H = R.resample(self.freq, how=lambda s: s.mean() / s.std() * np.sqrt(tools.freq(X.index)))

        # calculate features
        XX = self.featurize(H)
        yy = H.stack()

        # align training data and drop missing values
        XX = XX.dropna()
        yy = yy.dropna()
        XX = XX.ix[yy.index].dropna()
        yy = yy.ix[XX.index]

        # fit model on historical data
        self.model.fit(XX, yy)
        # print(self.model.intercept_, pd.Series(self.model.coef_, index=XX.columns))

        # make predictions for all assets with features
        XX_pred = XX.ix[XX.index[-1][0]]
        pred_sh = self.model.predict(XX_pred)
        pred_sh = pd.Series(pred_sh, index=XX_pred.index)

        # assume 0.5 sharpe for assets with missing features
        pred_sh = pred_sh.reindex(X.columns).fillna(0.5)

        # convert predictions from sharpe ratio to means
        mu = pred_sh * np.diag(sigma)
        return mu


class SingleIndexCovariance(BaseEstimator):
    """ Estimation of covariance matrix by Ledoit and Wolf (http://www.ledoit.net/ole2.pdf).
    It combines sample covariance matrix with covariance matrix from single-index model and
    automatically estimates shrinking parameter alpha.
    Assumes that first column represents index.
    """

    def __init__(self, alpha=None):
        self.alpha = alpha

    def _sample_covariance(self, X):
        return EmpiricalCovariance().fit(X).covariance_

    def _single_index_covariance(self, X, S):
        # estimate beta from CAPM (use precomputed sample covariance to calculate beta)
        # https://en.wikipedia.org/wiki/Simple_linear_regression#Fitting_the_regression_line
        var_market = S[0,0]
        y = X[:,0]
        beta = S[0,:] / var_market
        alpha = np.mean(X, 0) - beta * np.mean(y)

        # get residuals and their variance
        eps = X - alpha - np.matrix(y).T * np.matrix(beta)
        D = np.diag(np.var(eps, 0))

        return var_market * np.matrix(beta).T * np.matrix(beta) + D

    def _P(self, X, S):
        Xc = X - np.mean(X, 0)
        T, N = X.shape
        P = np.zeros((N, N))
        for i in range(N):
            for j in range(i, N):
                P[i,j] = P[j,i] = sum((Xc[:,i] * Xc[:,j] - S[i,j])**2)
        return P / T

    def _rho(self, X, S, F, P):
        Xc = X - np.mean(X, 0)
        T, N = X.shape
        R = np.zeros((N, N))
        for i in range(N):
            for j in range(i, N):
                g = (S[j,0] * S[0,0] * Xc[:,i] + S[i,0] * S[0,0] * Xc[:,j] - S[i,0]*S[j,0] * Xc[:,0]) / S[0,0]**2
                R[i,j] = R[j,i] = 1./T * sum(g * Xc[:,0] * Xc[:,i] * Xc[:,j] - F[i,j] * S[i,j])
        return np.sum(R)

    def _gamma(self, S, F):
        return np.sum((F - S)**2)

    def _optimal_alpha(self, X, S, F):
        T = X.shape[0]
        P = self._P(X, S)
        phi = np.sum(P)
        gamma = self._gamma(S, F)
        rho = self._rho(X, S, F, P)
        return 1./T * (phi - rho) / gamma

    def fit(self, X):
        # use implicitely with arrays
        X = np.array(X)

        # sample and single-index covariance
        S = self._sample_covariance(X)
        F = self._single_index_covariance(X, S)

        alpha = self.alpha or self._optimal_alpha(X, S, F)
        S_hat = alpha * F + (1 - alpha) * S
        self.covariance_ = S_hat
        self.optimal_alpha_ = alpha
        return self


class HistoricalSharpeEstimator(object):

    def __init__(self, window=None, alpha=1e10, override_sharpe=None, prior_sharpe=0.3, max_sharpe=100.,
                 max_mu=100.):
        self.window = window
        self.alpha = alpha
        self.prior_sharpe = prior_sharpe
        self.max_sharpe = max_sharpe
        self.max_mu = max_mu
        self.override_sharpe = override_sharpe or {}

    def fit(self, X, sigma):
        if self.window:
            X = X.iloc[-self.window:]

        # get mean and variance of sharpe ratios
        mu_sh = tools.sharpe(X)
        var_sh = tools.sharpe_std(X) ** 2

        # combine prior sharpe ratio with observations
        alpha = self.alpha
        est_sh = (mu_sh / var_sh + self.prior_sharpe * alpha) / (1. / var_sh + alpha)
        est_sh = np.minimum(est_sh, self.max_sharpe)

        # override sharpe ratios
        for k, v in self.override_sharpe.items():
            if k in est_sh:
                est_sh[k] = v

        mu = est_sh * pd.Series(np.sqrt(np.diag(sigma)), index=sigma.index)
        mu = np.minimum(mu, self.max_mu)
        # print(est_sh[{'XIV', 'ZIV', 'UGAZ'} & set(est_sh.index)].to_dict())
        return mu


def ar(vals, frac):
    r = list(vals[:1])
    for v in vals[1:]:
        r.append(frac * r[-1] + v)
    return r


class FractionalCovariance(covariance.OAS):

    def __init__(self, frac, *args, **kwargs):
        self.frac = frac
        super().__init__(*args, **kwargs)

    def fit(self, Y):
        # calculate fractional returns
        logY = np.log(Y)
        fracY = ar(logY, self.frac)
        return super().fit(fracY)


class JPMEstimator(object):

    def __init__(self, year=2020, rfr=0., verbose=False):
        self.rfr = rfr
        self.verbose = verbose
        self.year = year
        self.col_ret = f'Arithmetic Return {year}'

    def _parse_jpm(self):
        # load excel
        df = pd.read_excel(f'data/jpm-matrix-usd-{self.year}.xlsx', skiprows=7)
        df.columns = ['class', 'asset', f'Compound Return {self.year}', self.col_ret, 'Annualized Volatility', f'Compound Return {self.year - 1}'] + list(df.columns[6:])
        df['class'] = df['class'].fillna(method='ffill')

        # correlation matrix
        corr = df.iloc[:, 6:]
        corr.index = df.asset
        corr.columns = df.asset
        corr = corr.fillna(corr.T)

        # returns matrix
        rets = df.iloc[:, 1:6].set_index('asset')
        rets = rets.replace({'-': None}).astype(float) / 100

        # fix names
        rets.index = [c.replace('\xa0', ' ') for c in rets.index]
        corr.index = [c.replace('\xa0', ' ') for c in corr.index]
        corr.columns = [c.replace('\xa0', ' ') for c in corr.columns]

        rets['Sharpe'] = (rets[self.col_ret] - rets.loc['U.S. Cash', self.col_ret]) / rets['Annualized Volatility']

        return rets, corr

    def jpm_map(self):
        jpm = {}
        for k, syms in JPM_MAP.items():
            jpm[k] = k
            for sym in syms:
                jpm[sym] = k
        return jpm

    def simulate(self, S):
        # simulate assets from JPM
        rets, corr = self._parse_jpm()

        freq = tools.freq(S.index)
        mean = rets[self.col_ret] / freq
        vols = rets['Annualized Volatility'] / np.sqrt(freq)
        cov = corr * np.outer(vols, vols)
        Y = np.random.multivariate_normal(mean, cov, size=len(S))

        Y = pd.DataFrame(1 + Y, columns=mean.index, index=S.index).cumprod()

        # all values should end with 1
        return Y / Y.iloc[-1]

    def plot(self):
        rets, corr = self._parse_jpm()
        layout = go.Layout(
            yaxis={'range': [0, rets[self.col_ret].max() * 1.1]},
            hovermode='closest',
            height=800,
            width=800,
        )
        rets.iplot(kind='scatter', mode='markers', x='Annualized Volatility', y=self.col_ret, text=list(rets.index), layout=layout)


class JPMMeanEstimator(JPMEstimator):

    def __init__(self, override_mean=None, **kwargs):
        self.override_mean = override_mean
        super().__init__(**kwargs)

    def fit(self, X, sigma):
        rets, _ = self._parse_jpm()

        sh = rets['Sharpe']

        # calculate sharpe ratio for assets
        jpm = self.jpm_map()
        sh = {col: sh[jpm[col]] for col in X.columns}

        self.se = SharpeEstimator(override_sharpe=sh, rfr=self.rfr, verbose=False)
        rets = self.se.fit(X, sigma)

        if self.override_mean:
            for k, v in self.override_mean.items():
                rets.loc[k] = v

        if self.verbose:
            print(pd.DataFrame({
                'volatility': np.sqrt(np.diag(sigma)),
                'mean': rets,
            }))

        assert set(X.columns) <= set(rets.index)
        return rets[X.columns]


class JPMCovEstimator(JPMEstimator):

    def __init__(self, window=None, use_jpm_volatility=False):
        self.window = window
        self.use_jpm_volatility = use_jpm_volatility
        super().__init__()

    def fit(self, X, ):
        rets, corr = self._parse_jpm()

        jpm = self.jpm_map()

        if set(X.columns) - set(jpm.keys()):
            raise Exception(f'{set(X.columns) - set(jpm.keys())} are missing from JPM_MAP')
        ix = [jpm[c] for c in X.columns]
        corr = corr.loc[:, ix].loc[ix, :]
        corr.index = X.columns
        corr.columns = X.columns
        vols = rets.loc[ix, 'Annualized Volatility']

        if not self.use_jpm_volatility:
            if self.window:
                X = X.iloc[-self.window:]
            vols = X.std() * np.sqrt(tools.freq(X.index))

        # create covariance matrix from correlation
        cov = corr * np.outer(vols, vols)

        # cov.loc['CASH', 'CASH'] = 0.000001

        assert set(X.columns) <= set(cov.index)
        return cov.loc[X.columns, X.columns]


JPM_MAP = {
    'U.S. Cash': ('CASH',),
    'U.S. Intermediate Treasuries': ('IEF', 'ZN', 'TYD'),
    'U.S. Long Treasuries': ('TLT', 'TMF'),
    'TIPS': (),
    'U.S. Aggregate Bonds': (),
    'U.S. Short Duration Government/Credit': (),
    'U.S. Long Duration Government/Credit': (),
    'U.S. Inv Grade Corporate Bonds': (),
    'U.S. Long Corporate Bonds': (),
    'U.S. High Yield Bonds': ('HYG',),
    'U.S. Leveraged Loans': ('BKLN',),
    'World Government Bonds hedged': (),
    'World Government Bonds': (),
    'World ex-U.S. Government Bonds hedged': (),
    'World ex-U.S. Government Bonds': (),
    'Emerging Markets Sovereign Debt': (),
    'Emerging Markets Local Currency Debt': ('LEMB',),
    'Emerging Markets Corporate Bonds': ('CEMB',),
    'U.S. Muni 1-15 Yr Blend': (),
    'U.S. Muni High Yield': ('HYD',),
    'U.S. Large Cap': ('SPY', 'TQQQ'),
    'U.S. Mid Cap': (),
    'U.S. Small Cap': ('IWM',),
    'Euro Area Large Cap': (),
    'Japanese Equity': ('EWJ',),
    'Hong Kong Equity': (),
    'UK Large Cap': (),
    'EAFE Equity hedged': ('DBEF',),
    'EAFE Equity': (),
    'Emerging Markets Equity': ('VWO',),
    'AC Asia ex-Japan Equity': ('AAXJ',),
    'AC World Equity': (),
    'U.S. Equity Value Factor': (),
    'U.S. Equity Momentum Factor': (),
    'U.S. Equity Quality Factor': (),
    'U.S. Equity Minimum Volatility Factor': (),
    'U.S. Equity Dividend Yield Factor': (),
    'U.S. Equity Diversified Factor': (),
    'Global Convertible': (),
    'Global Credit Sensitive Convertible': (),
    'Private Equity': (),
    'U.S. Core Real Estate*': (),
    'U.S. Value-Added Real Estate*': (),
    'European ex-UK Core Real Estate*': (),
    'Asia Pacific Core Real Estate*': (),
    'U.S. REITs': (),
    'Global Infrastructure Equity': ('IGF',),
    'Global Infrastructure Debt': (),
    'Diversified Hedge Funds': (),
    'Event Driven Hedge Funds': (),
    'Long Bias Hedge Funds': (),
    'Relative Value Hedge Funds': (),
    'Macro Hedge Funds': (),
    'Direct Lending*': (),
    'Commodities*': (),
    'Gold*': (),
    # added 2020
    'U.S. Inflation': (),
    'U.S. Securitized': (),
    'U.S. Convertible Bond hedged': (),
    'Global Convertible Bond': (),
    'U.S. Core Real Estate': (),
    'Asia Pacific Core Real Estate': (),
    'U.S. Value-Added Real Estate': (),
    'Gold': (),
    'Direct Lending': (),
    'European ex-UK Core Real Estate': (),
    'Commodities': (),
}
