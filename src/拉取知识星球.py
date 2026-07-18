# -*- coding: utf-8 -*-

import argparse
import time
import re
from datetime import datetime
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

from pipeline_config import config_value, project_path
from stock_utils import dated_output_path, previous_trading_day


DEFAULT_URL = "https://wx.zsxq.com/group/15555851111822"


# =========================
# 文本清洗函数
# =========================
def clean_text(text):

    # 删除 #标签
    text = re.sub(r'#\S+', '', text)

    # 删除手机号
    text = re.sub(r'1[3-9]\d{9}', '', text)

    # 删除 emoji 和符号
    text = re.sub(
        r'[\U00010000-\U0010ffff]|[⭕☎️🌸➡️❗️⭐🔥✨👉👇📞]',
        '',
        text
    )

    # 删除多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


parser = argparse.ArgumentParser(description="抓取知识星球文字观点")
parser.add_argument("--url", default=DEFAULT_URL, help="知识星球页面地址")
parser.add_argument(
    "--stop-date",
    default=previous_trading_day().strftime("%Y-%m-%d"),
    help="抓取截止日期 YYYY-MM-DD，默认上一个交易日（按周一至周五回退）",
)
parser.add_argument("--output", help="输出文本文件；默认按当前日期命名")
parser.add_argument(
    "--driver",
    default=str(project_path(config_value("files", "chromedriver", "src/bin/chromedriver.exe"))),
    help="ChromeDriver 路径",
)
parser.add_argument(
    "--auto-start",
    action="store_true",
    help="不等待命令行回车，登录后自动检测“文字观点”入口并继续（供 Web 控制台使用）",
)
parser.add_argument(
    "--login-timeout",
    type=int,
    default=300,
    help="自动等待登录的最长秒数，默认 300 秒",
)
args = parser.parse_args()

STOP_DATE = datetime.strptime(args.stop_date, "%Y-%m-%d")
print(f"本次抓取截止日期：{STOP_DATE:%Y-%m-%d}")
OUTPUT_FILE = args.output or dated_output_path(
    project_path(config_value("files", "output_dir", "data/output")),
    "zsxq",
    suffix=".txt",
)
Path(OUTPUT_FILE).parent.mkdir(parents=True, exist_ok=True)

options = Options()
service = Service(args.driver)

driver = webdriver.Chrome(service=service, options=options)
driver.get(args.url)

viewpoint_xpath = "//*[contains(text(),'文字观点')]"
if args.auto_start:
    print(f"浏览器已打开，请登录知识星球；最长等待 {args.login_timeout} 秒……")
    try:
        viewpoint = WebDriverWait(driver, args.login_timeout).until(
            lambda current: current.find_element(By.XPATH, viewpoint_xpath)
        )
    except Exception:
        driver.quit()
        raise RuntimeError(f"等待登录超时（{args.login_timeout} 秒），未找到“文字观点”入口")
else:
    print("请登录知识星球，然后按回车继续...")
    input()
    viewpoint = driver.find_element(By.XPATH, viewpoint_xpath)

print("已检测到登录，进入文字观点")
viewpoint.click()

time.sleep(5)


seen=set()
results=[]

same_round=0
loop=0


while True:

    loop+=1
    print("\n===== 循环",loop,"=====")


    # 点击展开全部
    buttons=driver.find_elements(By.XPATH,"//*[contains(text(),'展开全部')]")

    for b in buttons:
        try:
            b.click()
        except:
            pass


    posts=driver.find_elements(By.CSS_SELECTOR,"div.content")

    print("当前检测到帖子:",len(posts))


    new_count=0


    for p in posts:

        try:

            text=p.text.strip()

            if not text:
                continue


            parent=p.find_element(
                By.XPATH,
                "./ancestor::div[.//div[contains(@class,'date')]][1]"
            )

            date_elem=parent.find_element(By.CSS_SELECTOR,"div.date")

            time_str=date_elem.text.strip()

            post_time=datetime.strptime(time_str,"%Y-%m-%d %H:%M")

        except:
            continue


        if text in seen:
            continue


        seen.add(text)

        new_count+=1

        print("抓取:",time_str)


        # ===== 文本清洗 =====
        text = clean_text(text)


        results.append(time_str+"\n"+text)


        if post_time < STOP_DATE:

            print("达到截止日期:",time_str)

            with open(OUTPUT_FILE,"w",encoding="utf-8") as f:

                for r in results:
                    f.write(r+"\n\n")

            print("抓取完成，共",len(results),"条")

            driver.quit()
            exit()


    print("本轮新增:",new_count)


    if new_count==0:
        same_round+=1
    else:
        same_round=0


    if same_round>5:

        print("检测到滚动到底")

        with open(OUTPUT_FILE,"w",encoding="utf-8") as f:

            for r in results:
                f.write(r+"\n\n")

        print("抓取完成，共",len(results),"条")

        driver.quit()
        break


    # 滚动
    driver.execute_script("window.scrollBy(0,5000);")

    time.sleep(2)
