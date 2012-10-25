#coding:utf8

import logging
import time
import re
import traceback
from locale import getdefaultlocale
from datetime import datetime
from threading import Thread, Lock
from Queue import Queue,Empty
from urlparse import urljoin,urlparse
from collections import deque

import requests
from bs4 import BeautifulSoup 

from options import parser
from database import Database


#logger是全局的,线程安全
logger = logging.getLogger()

def congifLogger(logFile, logLevel):
    '''配置logging的日志文件以及日志的记录等级'''
    LEVELS={
        1:logging.CRITICAL, 
        2:logging.ERROR,
        3:logging.WARNING,
        4:logging.INFO,
        5:logging.DEBUG,#数字最大记录最详细
        }
    formatter = logging.Formatter(
        '%(asctime)s %(threadName)s %(levelname)s %(message)s')
    try:
        fileHandler = logging.FileHandler(logFile)
    except IOError, e:
        return False
    else:
        fileHandler.setFormatter(formatter)
        logger.addHandler(fileHandler)
        logger.setLevel(LEVELS.get(logLevel))
        return True


class Worker(Thread):

    def __init__(self, threadPool):
        Thread.__init__(self)
        self.threadPool = threadPool
        self.daemon = True
        self.state = None
        self.start()

    def stop(self):
        self.state = 'STOP'

    def run(self):
        while 1:
            if self.state == 'STOP':
                break
            try:
                func, args, kargs = self.threadPool.getTask(timeout=1)
            except Empty:
                continue
            try:
                self.threadPool.increaseRunsNum() 
                result = func(*args, **kargs) 
                self.threadPool.decreaseRunsNum()
                if result:
                    self.threadPool.putTaskResult(*result)
                self.threadPool.taskDone()
            except Exception, e:
                logger.critical(traceback.format_exc())


class ThreadPool(object):

    def __init__(self, threadNum):
        self.pool = [] #线程池
        self.threadNum = threadNum  #线程数
        self.lock = Lock() #线程锁
        self.running = 0    #正在run的线程数
        self.taskQueue = Queue() #任务队列
        self.resultQueue = Queue() #结果队列
    
    def startThreads(self):
        for i in range(self.threadNum): 
            self.pool.append(Worker(self))
    
    def stopThreads(self):
        for thread in self.pool:
            thread.stop()
            thread.join()
        del self.pool[:]
    
    def putTask(self, func, *args, **kargs):
        self.taskQueue.put((func, args, kargs))

    def getTask(self, *args, **kargs):
        task = self.taskQueue.get(*args, **kargs)
        return task

    def taskJoin(self, *args, **kargs):
        self.taskQueue.join()

    def taskDone(self, *args, **kargs):
        self.taskQueue.task_done()

    def putTaskResult(self, *args):
        self.resultQueue.put(args)

    def getTaskResult(self, *args, **kargs):
        return self.resultQueue.get(*args, **kargs)

    def increaseRunsNum(self):
        self.lock.acquire() #锁住该变量,保证操作的原子性
        self.running += 1 #正在运行的线程数加1
        self.lock.release()

    def decreaseRunsNum(self):
        self.lock.acquire() 
        self.running -= 1 
        self.lock.release()

    def getTaskLeft(self):
        #线程池的所有任务包括：
        #taskQueue中未被下载的任务, resultQueue中完成了但是还没被取出的任务, 正在运行的任务
        #因此任务总数为三者之和
        return self.taskQueue.qsize()+self.resultQueue.qsize()+self.running


class WebPage(object):

    def __init__(self, url):
        self.url = url
        self.customeHeaders()

    def get(self, retry=2):
        '''获取html源代码'''
        try:
            #设置了prefetch=False，当访问response.text时才下载网页内容,避免下载非html文件
            response = requests.get(self.url, headers=self.headers, timeout=10, prefetch=False)
            if self._isResponseAvaliable(response):
                logger.debug('Get Page from : %s \n' % self.url)
                self._handleEncoding(response)
                return response.text
            else:
                logger.warning('Page not avaliable. Status code:%d URL: %s \n' % (
                    response.status_code, self.url) )
        except Exception,e:
            if retry>0: #超时重试
                return self.get(retry-1)
            else:
                logger.debug(str(e) + ' URL: %s \n' % self.url)
                logger.error(traceback.format_exc()+'URL:%s' % self.url)
        return None

    def customeHeaders(self, **kargs):
        #自定义header,防止被禁,某些情况如豆瓣,还需制定cookies,否则被ban        
        self.headers = {
            'Accept' : 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Charset' : 'gb18030,utf-8;q=0.7,*;q=0.3',
            'Accept-Encoding' : 'gzip,deflate,sdch',
            'Accept-Language' : 'en-US,en;q=0.8',
            'Connection': 'keep-alive',
            #'Host':urlparse(self.url).hostname, 会导致TooManyRedirects, 因为hostname不变
            'User-Agent' : 'Mozilla/5.0 (X11; Linux i686) AppleWebKit/537.4 (KHTML, like Gecko) Chrome/22.0.1229.79 Safari/537.4',
            'Referer' : self.url,
        }
        self.headers.update(kargs)

    def _isResponseAvaliable(self, response):
        #网页为200时再获取源码 (requests自动处理跳转)。只选取html页面。 
        if response.status_code == requests.codes.ok:
            if 'html' in response.headers['Content-Type']:
                return True
        return False

    def _handleEncoding(self, response):
        #requests会自动处理编码问题.
        #但是当header没有指定charset时,会使用RFC2616标准，指定编码为ISO-8859-1
        #因此会再从网页源码中的meta标签中的charset去判断编码
        if response.encoding == 'ISO-8859-1':
            charset_re = re.compile("((^|;)\s*charset=)([^\"]*)", re.M)
            charset=charset_re.search(response.text) 
            charset=charset and charset.group(3) or None 
            response.encoding = charset


class Crawler(object):

    def __init__(self, args):
        self.depth = args.depth  #指定网页深度
        self.currentDepth = 1  #标注初始爬虫深度，从1开始
        self.keyword = args.keyword.decode(getdefaultlocale()[1]) #指定关键词,使用console的默认编码来解码
        self.database =  Database(args.dbFile)#数据库
        self.threadPool = ThreadPool(args.threadNum)  #线程池,指定线程数
        self.visitedHrefs = set()    #已访问的链接
        self.unvisitedHrefs = deque()    #待访问的链接
        self.unvisitedHrefs.append(args.url) #添加首个待访问的链接
        self.isCrawling = False

    def start(self):
        print '\nStart Crawling\n'
        if not self._isDatabaseAvaliable():
            print 'Error: Unable to open database file.\n'
        else:
            self.isCrawling = True
            self.threadPool.startThreads() 
            while self.currentDepth < self.depth+1:
                #分配任务,线程池并发下载当前深度的所有页面（该操作不阻塞）
                self._assignCurrentDepthTasks()
                #等待当前线程池完成所有任务
                #self.threadPool.taskJoin()可代替以下操作，可无法Ctrl-C Interupt
                while self.threadPool.getTaskLeft():
                    time.sleep(8)
                #当池内的所有任务完成时，即代表爬完了一个网页深度
                print 'Depth %d Finish. Totally visited %d links. \n' % (
                    self.currentDepth, len(self.visitedHrefs))
                logger.info('Depth %d Finish. Total visited Links: %d\n' % (
                    self.currentDepth, len(self.visitedHrefs)))
                #迈进下一个深度
                self.currentDepth += 1
            self.stop()

    def stop(self):
        self.isCrawling = False
        self.threadPool.stopThreads()
        self.database.close()

    def _assignCurrentDepthTasks(self):
        while self.unvisitedHrefs:
            url = self.unvisitedHrefs.popleft()
            self.threadPool.putTask(self._taskHandler, url) #向任务队列分配任务
            self.visitedHrefs.add(url)  #标注该链接已被访问,或即将被访问,防止重复访问相同链接
 
    def _taskHandler(self, url):
        #先拿网页源码，再保存,两个都是高阻塞的操作，交给线程处理
        pageSource = WebPage(url).get()
        if pageSource:
            self._saveTaskResults(url, pageSource)
            self._addUnvisitedHrefs(url, pageSource)

    def _saveTaskResults(self, url, pageSource):
        try:
            if self.keyword:
                #使用正则的不区分大小写search比使用lower()后再查找要高效率(?)
                if re.search(self.keyword, pageSource, re.I):
                    self.database.saveData(url, pageSource, self.keyword) 
            else:
                self.database.saveData(url, pageSource)
        except Exception, e:
            logger.error(' URL: %s ' % url + traceback.format_exc())

    def _addUnvisitedHrefs(self, url, pageSource):
        '''添加未访问的链接。将有效的url放进UnvisitedHrefs列表'''
        #对链接进行过滤:1.只获取http或https网页;2.保证每个链接只访问一次
        hrefs = self._getAllHrefsFromPage(url, pageSource)
        for href in hrefs:
            if self._isHttpOrHttpsProtocol(href):
                if not self._isHrefRepeated(href):
                    self.unvisitedHrefs.append(href)

    def _getAllHrefsFromPage(self, url, pageSource):
        '''解析html源码，获取页面所有链接。返回链接列表'''
        hrefs = []
        soup = BeautifulSoup(pageSource)
        results = soup.find_all('a',href=True)
        for a in results:
            #必须将链接encode为utf8, 因为中文文件链接如 http://aa.com/文件.pdf 
            #在bs4中不会被自动url编码，从而导致encodeException
            href = a.get('href').encode('utf8')
            if not href.startswith('http'):
                href = urljoin(url, href)#处理相对链接的问题
            hrefs.append(href)
        return hrefs

    def _isHttpOrHttpsProtocol(self, href):
        protocal = urlparse(href).scheme
        if protocal == 'http' or protocal == 'https':
            return True
        return False

    def _isHrefRepeated(self, href):
        if href in self.visitedHrefs or href in self.unvisitedHrefs:
            return True
        return False

    def getAlreadyVisitedNum(self):
        #visitedHrefs保存已经分配给taskQueue的链接，有可能链接还在处理中。
        #因此真实的已访问链接数为visitedHrefs数减去待访问的链接数
        return len(self.visitedHrefs) - self.threadPool.getTaskLeft()

    def _isDatabaseAvaliable(self):
        if self.database.isConn():
            return True
        return False

    def selfTesting(self, args):
        url = 'http://www.baidu.com/'
        print '\nVisiting www.baidu.com'
        #测试网络,能否顺利获取百度源码
        pageSource = WebPage(url).get()
        if pageSource == None:
            print 'Please check your network and make sure it\'s connected.\n'
        #测试日志保存
        elif not congifLogger(args.logFile, args.logLevel):
            print 'Permission denied: %s' % args.logFile
            print 'Please make sure you have the permission to save the log file!\n'
        #数据库测试
        elif not self._isDatabaseAvaliable():
            print 'Please make sure you have the permission to save data: %s\n' % args.dbFile
        #保存数据
        else:
            self._saveTaskResults(url, pageSource)
            print 'Create logfile and database Successfully.'
            print 'Already save Baidu.com, Please check the database record.'
            print 'Seems No Problem!\n'


class PrintProgress(Thread):
    '''每隔10秒在屏幕上打印爬虫进度信息'''

    def __init__(self, crawler):
        Thread.__init__(self)
        self.beginTime = datetime.now()
        self.crawler = crawler
        self.daemon = True

    def run(self):
        while 1:
            if self.crawler.isCrawling:
                print '-------------------------------------------'
                print 'Crawling in depth %d' % self.crawler.currentDepth
                print 'Already visited %d Links' % self.crawler.getAlreadyVisitedNum()
                print '%d tasks remaining in thread pool.' % self.crawler.threadPool.getTaskLeft()
                print '-------------------------------------------\n'   
                time.sleep(10)

    def printSpendingTime(self):
        self.endTime = datetime.now()
        print 'Begins at :%s' % self.beginTime
        print 'Ends at   :%s' % self.endTime
        print 'Spend time: %s \n'%(self.endTime - self.beginTime)
        print 'Finish!'


def main():
    args = parser.parse_args()
    if args.testSelf:
        Crawler(args).selfTesting(args)
    elif not congifLogger(args.logFile, args.logLevel):
        print '\nPermission denied: %s' % args.logFile
        print 'Please make sure you have the permission to save the log file!\n'
    else:
        crawler = Crawler(args)
        printProgress = PrintProgress(crawler)
        printProgress.start()
        crawler.start()
        printProgress.printSpendingTime()

#!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
#TODO 把数据库和selfTesting 从爬虫类抽取出来～！
#TODO 还要整理一下文件权限验证的问题，现在的顺序和组织结构有问题
#TODO keyboardInterrupt 的处理？
#TODO 链接问题处理 /////baidu.com
#TODO 爬虫被ban的话，如何处理？
#!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        
if __name__ == '__main__':
    main()