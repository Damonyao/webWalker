# -*- coding:utf-8 -*-
import sys
reload(sys)
sys.setdefaultencoding('utf-8')

import time
import socket
from urlparse import urlparse, urljoin

from scrapy import Field, Item, signals
from scrapy.exceptions import DontCloseSpider
from scrapy.http import Request, FormRequest
from scrapy.spiders import Spider
from scrapy.utils.response import response_status_message

from exception_process import parse_method_wrapper, parse_next_method_wrapper
from utils import get_ip_address, url_arg_increment, Logger, get_val,\
     url_item_arg_increment, url_path_arg_increment, LoggerDiscriptor


BASE_FIELD = ["success", "domain", "exception", "crawlid", "spiderid", "workerid", "response_url", "status_code", "status_msg", "url", "seed", "timestamp"]
ITEM_FIELD = {}
ITEM_XPATH = {}
PAGE_XPATH = {}


@parse_next_method_wrapper
def next_request_callback(self, response):
    k = response.meta.get("next_key")
    self.logger.debug("start in parse %s ..." % k)
    filed_list = ITEM_FIELD[self.name]
    v = filter(lambda x:x, map(lambda x:x if x[0] == k else "", filed_list))[0][1]
    item = self.reset_item(response.meta['item_half'])
    item[k] = get_val(v, response, item, is_after=True) or v.get("default", "")
    self.logger.info("crawlid:%s, product_id %s, suceessfully yield item"%(item.get("crawlid"), item.get("product_id", "unknow")))
    self.crawler.stats.inc_crawled_pages(response.meta['crawlid'])

    return item


def send_request_wrapper(response, item, k):

    def process_request(func):

        def wrapper():
            url, cookies, method, body = func(item, response)
            response.meta['item_half'] = dict(item)
            response.meta['next_key'] = k
            response.meta["priority"] += 1

            if cookies:
                response.meta["cookie"] = cookies
                response.meta["dont_update_cookies"] = True

            if url and method == "post":
                return FormRequest(
                    url=url,
                    callback=next_request_callback,
                    formdata=body,
                    meta=response.meta,
                    dont_filter=True)
            if url:
                return Request(
                        url=url,
                        meta=response.meta,
                        callback=next_request_callback,
                        dont_filter=response.request.dont_filter,
                    )

        return wrapper

    return process_request


class ClusterSpider(Spider, Logger):

    name = "cluster_spider"
    next_request_callback = next_request_callback
    proxy = None
    change_proxy = None
    logger = LoggerDiscriptor()

    def __init__(self, *args, **kwargs):

        Spider.__init__(self, *args, **kwargs)
        self.worker_id = ("%s_%s" % (socket.gethostname(), get_ip_address())).replace('.', '_')
        self.gen_field = self._yield_field()
        self.base_item_cls = type("RawResponseItem", (Item, ),
                                  dict(zip(BASE_FIELD, self.gen_field)))

    def _set_crawler(self, crawler):

        Spider._set_crawler(self, crawler)
        self.crawler.signals.connect(self.spider_idle,
                                     signal=signals.spider_idle)


    def spider_idle(self):

        print('Dont close spider........')
        raise DontCloseSpider

    def set_logger(self, crawler):

        Logger.set_logger(self, crawler)

    def set_redis(self, redis_conn):

        self.redis_conn = redis_conn

    def _yield_field(self):

        while True:
            yield Field()

    def get_item_cls(self):

        return type("%sItem"%self.name.capitalize(), (self.base_item_cls, ),
                    dict(zip(map(lambda x:x[0], ITEM_FIELD[self.name]), self.gen_field)))

    def reset_item(self, dict):

        item = self.get_item_cls()()

        for key in dict.keys():
            item[key] = dict[key]

        return item

    def common_property(self, response, item):

        for k, v in ITEM_FIELD[self.name]:
            val = get_val(v, response, item)

            if not val:
                request_func = v.get("request")

                if request_func:
                    request = send_request_wrapper(response, item, k)(request_func)()
                    if request:
                        return request

            item[k] = val or v.get("default", "")

    @parse_method_wrapper
    def parse(self, response):

        self.logger.debug("start response in parse")
        item_urls = [urljoin(response.url, x) for x in set(response.xpath("|".join(ITEM_XPATH[self.name])).extract())]
        self.crawler.stats.inc_total_pages(response.meta['crawlid'], len(item_urls))

        if "if_next_page" in response.meta:
            del response.meta["if_next_page"]
        else:
            response.meta["seed"] = response.url

        # 防止代理继承  add at 16.10.26
        response.meta.pop("proxy", None)
        response.meta["callback"] = "parse_item"
        response.meta["priority"] -= 20

        for item_url in item_urls:
            response.meta["url"] = item_url
            yield Request(url=item_url,
                          callback=self.parse_item,
                          meta=response.meta,
                          errback=self.errback)

        xpath = "|".join(PAGE_XPATH[self.name])

        if xpath.count("?") == 1:
            next_page_urls = [url_arg_increment(xpath, response.url)] if len(item_urls) else []
        elif xpath.count("subpath="):
            next_page_urls = [url_path_arg_increment(xpath, response.url)] if len(item_urls) else []
        elif xpath.count("/") > 1:
            next_page_urls = [urljoin(response.url, x) for x in set(response.xpath(xpath).extract())]
        else:
            next_page_urls = [url_item_arg_increment(xpath, response.url, len(item_urls))] if len(item_urls) else []

        response.meta["if_next_page"] = True
        response.meta["callback"] = "parse"
        response.meta["priority"] += 20

        for next_page_url in next_page_urls:
            response.meta["url"] = next_page_url
            yield Request(url=next_page_url,
                          callback=self.parse,
                          meta=response.meta)

    @parse_method_wrapper
    def parse_item(self, response):

        self.logger.debug("start response in parse_item")
        item = self._enrich_base_data(response)
        request = self.common_property(response, item)

        if getattr(self, 'have_duplicate', False):
            result = self.total_pages_decrement(response, item.get("product_id"))

            if not result:
                return

        if request:
            return request

        self.logger.info("crawlid:%s, product_id: %s, suceessfully yield item" % (
        item.get("crawlid"), item.get("product_id", "unknow")))
        self.crawler.stats.inc_crawled_pages(response.meta['crawlid'])
        return item

    def total_pages_decrement(self, response, id):

        crawlid = response.meta["crawlid"]

        if self.redis_conn.sismember("crawlid:%s:model" % crawlid, id):
            self.crawler.stats.inc_total_pages(response.meta['crawlid'], -1)
            return False
        else:
            self.redis_conn.sadd("crawlid:%s:model" % crawlid, id)
            self.redis_conn.expire("crawlid:%s:model" % crawlid, self.crawler.settings.get("DUPLICATE_TIMEOUT", 60*60))
            return True

    def _enrich_base_data(self, response):
        item = self.get_item_cls()()
        item['spiderid'] = response.meta['spiderid']
        item['workerid'] = self.worker_id
        item['url'] = response.meta["url"]
        item["seed"] = response.meta.get("seed", "")
        item["timestamp"] = time.strftime("%Y%m%d%H%M%S")
        item['status_code'] = response.status
        item["status_msg"] = response_status_message(response.status)
        item['domain'] = urlparse(response.url).hostname.split(".", 1)[1]
        item['crawlid'] = response.meta['crawlid']
        item['response_url'] = response.url
        return item

    def errback(self, failure):

        if failure and failure.value and hasattr(failure.value, 'response'):
            response = failure.value.response

            if response:
                item = self._enrich_base_data(response)
                self.logger.error("errback: %s" % item)
                self.crawler.stats.inc_crawled_pages(response.meta['crawlid'])
                return item
            else:
                self.logger.error("failure has NO response")
        else:
            self.logger.error("failure or failure.value is NULL, failure: %s" % failure)


def start(spiders, globals, module_name, item_field, item_xpath, page_xpath):

    ITEM_FIELD.update(item_field)
    ITEM_XPATH.update(item_xpath)
    PAGE_XPATH.update(page_xpath)

    def create(k, v):
        v["__module__"] = module_name
        return type("%sSpider" % k, (ClusterSpider,), v)

    index = 0

    for k, v in spiders.items():
        v.update({"name": k})
        exec("cls_%s = create(k, v)"% index, locals(), globals)
        index += 1

