import requests
from bs4 import BeautifulSoup
import pandas as pd
import re
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed


def safe_int(text):
    """Convert a text like '1,234' / '558' to int, return None on failure."""
    if text is None:
        return None
    digits = re.sub(r"[^\d]", "", str(text))
    return int(digits) if digits else None


def scrape_page(page, base_url, headers):
    """Scrape a single page and return the HTML together with the first ranking on it."""
    url = f"{base_url}?page={page}"
    try:
        response = None
        for i in range(5):
            try:
                response = requests.get(url, headers=headers, timeout=30)
                if response.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(2 + i)

        if not response or response.status_code != 200:
            return page, None, None, f"Status: {response.status_code if response else 'Timeout'}"

        soup = BeautifulSoup(response.text, 'html.parser')
        table = soup.find('table')
        if not table:
            return page, None, None, "No table found"

        first_rank = _first_rank(table)

        return page, response.text, first_rank, None

    except Exception as e:
        return page, None, None, str(e)


def _first_rank(table):
    """Return the rank shown on the first data row of a table (None if not present)."""
    tbody = table.find('tbody')
    rows = tbody.find_all('tr') if tbody else table.find_all('tr')[1:]
    for row in rows:
        cols = row.find_all('td')
        if cols:
            rank = safe_int(cols[0].get_text(strip=True))
            if rank is not None:
                return rank
    return None


def parse_page(html):
    """Parse HTML content and extract table data."""
    if not html:
        return [], []

    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table')
    if not table:
        return [], []

    column_names = []
    thead = table.find('thead')
    if thead:
        column_names = [th.get_text(strip=True) for th in thead.find_all('th') if th.get_text(strip=True)]

    if not column_names:
        column_names = ["#", "CPU", "Single Score", "Multi Score", "Cores", "Max. TDP"]

    tbody = table.find('tbody')
    rows = tbody.find_all('tr') if tbody else table.find_all('tr')[1:]

    data = []
    for row in rows:
        cols = row.find_all('td')
        if len(cols) >= len(column_names):
            row_data = []
            for col in cols:
                text = col.get_text(strip=True)
                row_data.append(text if text else "null")
            data.append(row_data[:len(column_names)])

    return column_names, data


def _normalize_cpu_name(name):
    """Strip a trailing 'Desktop'/'Laptop' marker inside a CPU name and return (name, platform)."""
    if name.endswith('Desktop'):
        return name[:-7].strip(), 'Desktop'
    if name.endswith('Laptop'):
        return name[:-6].strip(), 'Laptop'
    return name, ''


def _extract_platform_map(html):
    """Map ranking -> Platform by reading the data-sort attribute of the CPU cell.

    The site stores the platform in the second column as data-sort="desktop"/"laptop",
    which is far more reliable than guessing from the CPU name.
    """
    platform_map = {}
    if not html:
        return platform_map

    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table')
    if not table:
        return platform_map

    tbody = table.find('tbody')
    rows = tbody.find_all('tr') if tbody else table.find_all('tr')[1:]
    for row in rows:
        cols = row.find_all('td')
        if len(cols) < 2:
            continue
        rank = safe_int(cols[0].get_text(strip=True))
        if rank is None:
            continue
        platform = (cols[1].get('data-sort') or '').strip().capitalize()
        if platform in ('Desktop', 'Laptop') and rank not in platform_map:
            platform_map[rank] = platform

    return platform_map


def scrape_nanoreview_cpu_scores(start_page=1, end_page=3, max_workers=3):
    """Scrape CPU Cinebench scores from nanoreview.net using multi-threading."""
    base_url = "https://nanoreview.net/en/cpu-list/cinebench-scores"
    all_data = []
    column_names = []
    platform_map = {}
    page_first_rank = {}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://nanoreview.net/"
    }

    print(f"开始抓取第 {start_page} 到第 {end_page} 页的数据 (使用 {max_workers} 线程)...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(scrape_page, page, base_url, headers): page for page in range(start_page, end_page + 1)}

        for future in as_completed(futures):
            page = futures[future]
            try:
                page_num, html, first_rank, error = future.result()
                if error:
                    print(f"第 {page_num} 页抓取失败: {error}")
                    continue

                page_first_rank[page_num] = first_rank
                platform_map.update(_extract_platform_map(html))

                cols, data = parse_page(html)
                if not column_names and cols:
                    column_names = cols

                if data:
                    all_data.extend(data)
                    print(f"第 {page_num} 页抓取成功，获取 {len(data)} 条记录。")
                else:
                    print(f"第 {page_num} 页无数据。")

                time.sleep(random.uniform(0.5, 1.0))

            except Exception as e:
                print(f"第 {page} 页处理异常: {e}")

    # 分页失效检测：?page=N 参数被忽略时，每一页返回的都是同一份第 1 页数据，
    # 直接合并就会得到 N 份完全相同的记录（默认 3 页 => 每条数据重复 3 次）。
    dup_factor = 1
    valid_ranks = [r for r in page_first_rank.values() if r is not None]
    if len(valid_ranks) > 1 and len(set(valid_ranks)) == 1:
        dup_factor = len(valid_ranks)
        print(
            f"\n[警告] 检测到第 {start_page}-{end_page} 页返回了相同的起始排名 (#{valid_ranks[0]})，"
            f"网站已忽略 ?page= 分页参数，仅保留第 1 页数据。"
        )

    if all_data:
        df = pd.DataFrame(all_data, columns=column_names)

        # 按Ranking排序
        ranking_col = 'Ranking' if 'Ranking' in df.columns else '#'
        if ranking_col in df.columns:
            df[ranking_col] = pd.to_numeric(df[ranking_col], errors='coerce')
            df = df.sort_values(by=ranking_col, ascending=True, kind='stable')

            # 去重保护：分页重复 / 站点本身重复时，仅保留首条记录
            before = len(df)
            df = df.drop_duplicates()
            if len(df) != before:
                print(f"已去除 {before - len(df)} 条完全重复的记录。")

            if dup_factor > 1:
                df = df.drop_duplicates(subset=[ranking_col], keep='first')
            df = df.reset_index(drop=True)

        # Rename "#" to "Ranking"
        if '#' in df.columns:
            df.rename(columns={'#': 'Ranking'}, inplace=True)

        # Extract Platform from CPU column (Desktop/Laptop) as a fallback
        df['CPU'] = df['CPU'].astype(str)
        parsed = df['CPU'].apply(_normalize_cpu_name)
        df['CPU'] = parsed.apply(lambda x: x[0])
        df['Platform'] = parsed.apply(lambda x: x[1])

        # Prefer the platform marker provided by the site itself
        if 'Ranking' in df.columns:
            site_platform = df['Ranking'].map(platform_map)
            df['Platform'] = site_platform.fillna(df['Platform'])

        df['Platform'] = df['Platform'].replace('', 'null')

        # Reorder columns: CPU, Platform, then the rest
        cols = df.columns.tolist()
        cols.remove('Platform')
        cols.insert(1, 'Platform')
        df = df[cols]

        # Save to CSV with UTF-8-SIG for Excel compatibility
        output_csv = "cpu_cinebench_data_1_3.csv"
        df.to_csv(output_csv, index=False, encoding='utf-8-sig')

        # Save to JSON
        output_json = "cinebench_scores.json"
        df.to_json(output_json, orient='records', force_ascii=False, indent=2)

        print("\n" + "=" * 30)
        print(f"抓取任务完成！")
        print(f"总计行数: {len(df)}")
        print(f"CSV保存路径: {output_csv}")
        print(f"JSON保存路径: {output_json}")
        print("=" * 30)
        return df
    else:
        print("未获取到任何有效数据。")
        return None


import subprocess


def git_push():
    try:
        files_to_push = [".gitignore", "cinebench.py", "cinebench_scores.json", "cpu_cinebench_data_1_3.csv"]
        for f in files_to_push:
            subprocess.run(["git", "add", f], check=True)
        result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if result.stdout.strip():
            subprocess.run(["git", "add", "-A"], check=True)
            subprocess.run(["git", "commit", "-m", "auto update data"], check=True)
            subprocess.run(["git", "push"], check=True)
            print("\n已自动推送到GitHub")
        else:
            print("\n无新更改，无需推送")
    except subprocess.CalledProcessError as e:
        print(f"Git操作失败: {e}")


if __name__ == "__main__":
    scrape_nanoreview_cpu_scores(1, 3, max_workers=3)
