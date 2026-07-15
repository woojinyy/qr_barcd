import os
import shutil
import csv
import requests
import re
import json
import time
import concurrent.futures
from datetime import datetime, timedelta
from tkinter import filedialog, Tk
from playwright.sync_api import sync_playwright

AUTH_FILE = "mes_auth_state.json"
SPECIAL_DB_FILE = "커스텀_PMMA_작업장부.csv"
MASTER_CACHE_FILE = "mes_data_master.json"

CLONED_URL = None
CLONED_HEADERS = {}

# ---------------------------------------------------------
# [1] 장부 처리 함수
# ---------------------------------------------------------
def log_special_work(actual_saved_name, file_qr, final_barcode, patient, clinic, tooth, type_name, target_path):
    file_exists = os.path.isfile(SPECIAL_DB_FILE)
    if file_exists:
        try:
            with open(SPECIAL_DB_FILE, mode='r', encoding='utf-8-sig') as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) > 1:
                        logged_filename = row[1].upper()
                        base_name = re.sub(r'_수정본\d*', '', actual_saved_name)
                        is_qr_match = file_qr and (file_qr in logged_filename)
                        is_bc_match = final_barcode and (final_barcode in logged_filename)
                        if (is_qr_match and is_bc_match) or (base_name in row[1]):
                            print(f"   ⚠️ [장부 기록 패스] 이미 장부에 동일한 내역이 존재합니다.")
                            return
        except: pass

    with open(SPECIAL_DB_FILE, mode='a', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['기록일시', '스캔파일명', '환자명', '치과명', '기공품목', '작업치식(Tooth)', 'NAS저장경로'])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([now, actual_saved_name, patient, clinic, type_name, tooth, target_path])
    print(f"   📝 [장부 처리 완료] {type_name} 품목 기록됨.")

# ---------------------------------------------------------
# [2] MES 데이터 싹쓸이 엔진
# ---------------------------------------------------------
def fetch_mes_data_live():
    global CLONED_URL, CLONED_HEADERS
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d %H:%M:%S")
    thirty_days_ago = today - timedelta(days=30)

    master_data = {}
    last_sync_time = None
    last_update_str = ""

    if os.path.exists(MASTER_CACHE_FILE):
        try:
            with open(MASTER_CACHE_FILE, 'r', encoding='utf-8') as f:
                cache_content = json.load(f)

            loaded_data = cache_content.get("data", {})
            last_update_str = cache_content.get("last_update", "")

            if len(last_update_str) >= 19:
                last_sync_time = datetime.strptime(last_update_str, "%Y-%m-%d %H:%M:%S")
            elif len(last_update_str) >= 10:
                last_sync_time = datetime.strptime(last_update_str[:10], "%Y-%m-%d")

            for uid, item in loaded_data.items():
                date_str = item.get("req_time") or item.get("create_time")
                if date_str and len(date_str) >= 10:
                    try:
                        if datetime.strptime(date_str[:10], "%Y-%m-%d") >= thirty_days_ago:
                            master_data[uid] = item
                    except: master_data[uid] = item
                else: master_data[uid] = item

            deleted_count = len(loaded_data) - len(master_data)
            if deleted_count > 0:
                print(f"\n🧹 [자동 청소] 30일이 지난 낡은 데이터 {deleted_count}건 삭제 완료!")
        except:
            pass

    print("\n" + "="*50)
    print(" 💡 [데이터 수집 모드 선택]")
    print("  1. 🚀 스마트 자동 (아침/점심 하루 2번만 서버 접속. 평소엔 0.01초 컷!)")
    print("  2. 🔄 강제 새로고침 (정렬 무시! 한 달 치 150페이지 딥스캔 싹쓸이)")
    print("  3. 🔍 과거 데이터 수동 스캔 (원하는 페이지 직접 입력)")
    print("="*50)

    mode = input("▶ 원하시는 작업 번호를 입력하세요 (1, 2, 3 중 선택, 기본값 1): ").strip()
    if mode not in ['1', '2', '3']:
        mode = '1'

    start_page = 1
    end_page = 30
    need_network_sync = True
    sync_reason = ""

    if mode == '1' and last_sync_time:
        is_same_day = (last_sync_time.date() == today.date())
        is_now_afternoon = (today.hour >= 13)
        was_sync_afternoon = (last_sync_time.hour >= 13)

        if is_same_day:
            if is_now_afternoon and not was_sync_afternoon:
                need_network_sync = True
                sync_reason = "점심시간이 지나 [오후 데이터 50페이지]를 갱신합니다."
                end_page = 50
            else:
                need_network_sync = False
        else:
            need_network_sync = True
            sync_reason = "날짜가 변경되어 [오늘 첫 데이터 50페이지]를 가져옵니다."
            end_page = 50

    elif mode == '2':
        need_network_sync = True
        sync_reason = "정렬 상태와 무관하게 한 달 치 [150페이지 전체 딥스캔]을 진행합니다."
        end_page = 150

    elif mode == '3':
        need_network_sync = True
        sync_reason = "[수동 과거 데이터 스캔]을 진행합니다."
        try:
            print("\n(참고: 1페이지당 약 100건의 데이터가 들어있습니다.)")
            start_page = int(input("▶ 시작할 페이지 번호 (예: 51): "))
            end_page = int(input("▶ 끝날 페이지 번호 (예: 200): "))
        except:
            print("❌ 숫자 입력이 잘못되어 강제 새로고침(2번)으로 진행합니다.")
            mode = '2'
            end_page = 150
            sync_reason = "오류 복구: 강제 150페이지 딥스캔을 진행합니다."

    elif not last_sync_time:
        need_network_sync = True
        sync_reason = "저장된 데이터가 없어 [최초 다운로드 150페이지]를 시작합니다."
        end_page = 150

    if not need_network_sync:
        print(f"\n⚡ [스마트 패스 발동] 마지막 동기화: {last_update_str}")
        print(f"⚡ 지루한 브라우저 통신을 건너뛰고 매칭을 즉시 시작합니다!")
        return list(master_data.values())

    captured_data = []
    print(f"\n🌐 [서버 접속] {sync_reason}")
    print("   브라우저를 띄워 통신 패킷을 캡처합니다...")

    with sync_playwright() as p:
        is_logged_in = os.path.exists(AUTH_FILE)
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=AUTH_FILE) if is_logged_in else browser.new_context()
        page = context.new_page()

        def clone_network_request(request):
            global CLONED_URL, CLONED_HEADERS
            if not CLONED_URL and request.method == "GET" and "mis/item" in request.url and "page=" in request.url and "page_size=" in request.url:
                CLONED_URL = request.url
                CLONED_HEADERS = {k: v for k, v in request.all_headers().items() if not k.startswith(':') and k.lower() != 'accept-encoding'}

        page.on("request", clone_network_request)

        try:
            if not is_logged_in:
                page.goto("https://mes.dentalsoft.co.kr/login")
                page.wait_for_timeout(60000)
                context.storage_state(path=AUTH_FILE)

            page.goto("https://mes.dentalsoft.co.kr/produce/enter")
            page.wait_for_load_state("networkidle")
            page.get_by_text("1개월 전").first.click()
            page.wait_for_timeout(1000)
            page.get_by_text("새로고침 (F5)").first.click()

            for _ in range(10):
                if CLONED_URL: break
                page.wait_for_timeout(500)
        except: pass
        finally: browser.close()

    if not CLONED_URL or not CLONED_HEADERS:
        print("   ❌ 패킷 복제 실패. 로컬 캐시를 사용합니다.")
        return list(master_data.values()) if master_data else []

    base_url = re.sub(r'page_size=\d+', 'page_size=100', CLONED_URL)

    def fetch_api_page(page_num):
        target_url = re.sub(r'page=\d+', f'page={page_num}', base_url)
        for _ in range(3):
            try:
                res = requests.get(target_url, headers=CLONED_HEADERS, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, list): return data
                    if isinstance(data, dict):
                        for v in data.values():
                            if isinstance(v, list): return v
                    return []
                time.sleep(0.5)
            except: time.sleep(0.5)
        return []

    def fetch_pages_in_batch(start_p, end_p):
        batch_data = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(fetch_api_page, i): i for i in range(start_p, end_p + 1)}
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res: batch_data.extend(res)
        return batch_data

    print(f"   🚀 조기 종료 없이 {start_page} ~ {end_page}페이지 전체를 무조건 스캔합니다...")
    captured_data = fetch_pages_in_batch(start_page, end_page)

    new_count = update_count = 0
    for item in captured_data:
        uid = item.get("item_id")
        if uid:
            if uid in master_data: update_count += 1
            else: new_count += 1
            master_data[uid] = item

    current_no = 1
    for uid, item in master_data.items():
        item["_데이터번호"] = current_no
        current_no += 1

    print(f"   🔄 수집 완료: [신규 {new_count}건 / 업데이트 {update_count}건] 추가됨 (총 {len(master_data)}건 보유)")

    with open(MASTER_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump({"last_update": today_str, "data": master_data}, f, ensure_ascii=False, indent=4)

    return list(master_data.values())

def select_source_folder():
    root = Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    print("\n📂 2단계: 작업할 원본 폴더를 선택하세요...")
    return filedialog.askdirectory(title="원본 경로 선택")

# ---------------------------------------------------------
# [3] 과잉 해석 차단 & 🚨 리메이크(A/S) 프리패스 탑재
# ---------------------------------------------------------
def start_safe_move_process():
    mes_api_data = fetch_mes_data_live()
    src_path = select_source_folder()
    if not src_path: return

    print(f"\n--- 이동, 매칭 시작 ---")
    items = os.listdir(src_path)

    for item_name in items:
        full_src = os.path.join(src_path, item_name)
        file_name_upper = item_name.upper()

        try:
            mtime = os.path.getmtime(full_src)
            if time.time() - mtime < 5.0:
                print(f"\n⏳ [안전 대기] '{item_name}'이(가) 아직 저장 중인 것 같습니다. 5초간 대기합니다...")
                time.sleep(5)
        except:
            pass

        clean_name = re.sub(r'\(\d+\)', '', os.path.splitext(item_name)[0])
        clean_name = clean_name.replace('- COPY', '').replace('복사본', '').replace('_수정본', '').strip()

        parts = re.split(r'[\s_]+', clean_name)
        file_qr = ""
        if parts:
            last_part = parts[-1]
            if len(last_part) == 4 and last_part.isalnum() and not last_part.isdigit():
                file_qr = last_part.upper()

        name_for_barcode = re.sub(r'20\d{2}-\d{2}-\d{2}', '', file_name_upper)
        name_for_barcode = re.sub(r'\d{6}_', '_', name_for_barcode)

        file_numbers = re.findall(r'\d+', name_for_barcode)
        file_numbers_int = [str(int(n)) for n in file_numbers]

        file_dt = None
        file_date_match = re.search(r'(20\d{2}-\d{2}-\d{2})', item_name)
        if file_date_match:
            try:
                file_dt = datetime.strptime(file_date_match.group(1), "%Y-%m-%d")
            except: pass

        print(f"\n🔍 분석 중: '{item_name}' (타겟 QR: {file_qr if file_qr else '없음'})")

        final_case = None
        all_matched_cases = []

        if mes_api_data and file_qr:
            qr_matched_cases = [c for c in mes_api_data if file_qr in str(c.get("desc", "")).upper()]
            if len(qr_matched_cases) == 1:
                final_case = qr_matched_cases[0]
                all_matched_cases = qr_matched_cases
                print(f"   🎯 [Track A] 고유 QR({file_qr})일치")
            elif len(qr_matched_cases) > 1:
                for case in qr_matched_cases:
                    barcode = str(case.get("barcode_id", "")).strip()
                    if barcode and len(barcode) >= 3 and (barcode in name_for_barcode or (barcode.isdigit() and str(int(barcode)) in file_numbers_int)):
                        all_matched_cases.append(case)
                if all_matched_cases:
                    final_case = all_matched_cases[0]
                    print(f"   🎯 [Track A] QR 중복 방어 -> 바코드  일치")

        if not final_case and mes_api_data:
            bc_matched_cases = []
            for case in mes_api_data:
                barcode = str(case.get("barcode_id", "")).strip()
                if barcode and len(barcode) >= 3 and (barcode in name_for_barcode or (barcode.isdigit() and str(int(barcode)) in file_numbers_int)):
                    bc_matched_cases.append(case)

            if len(bc_matched_cases) == 1:
                final_case = bc_matched_cases[0]
                all_matched_cases = bc_matched_cases
                print(f"   🚑 [Track B] 유일한 바코드({final_case.get('barcode_id')}) 매칭 완료.")
            elif len(bc_matched_cases) > 1:
                for case in bc_matched_cases:
                    if file_qr and file_qr in str(case.get("desc", "")).upper():
                        all_matched_cases.append(case)
                if all_matched_cases:
                    final_case = all_matched_cases[0]
                    print(f"   🚑 [Track B] 바코드 중복 방어 -> QR 일치")
                else:
                    final_case = bc_matched_cases[0]
                    all_matched_cases = [final_case]

        final_target_path = None

        if final_case:
            target_path_raw = str(final_case.get("desc", ""))

            if target_path_raw and target_path_raw != "None" and target_path_raw.strip() != "":
                path_start_idx = target_path_raw.find('\\\\')

                drive_match = None
                if path_start_idx == -1:
                    drive_match = re.search(r'[A-Za-z]:\\', target_path_raw)
                    if drive_match:
                        path_start_idx = drive_match.start()

                clean_desc = target_path_raw[path_start_idx:] if path_start_idx != -1 else target_path_raw
                clean_desc = clean_desc.replace('\n', ' ').replace('\r', ' ').replace('*', '')

                is_time_paradox = False

                if file_qr and file_qr not in clean_desc.upper():
                    print(f"   ⚠️ [경고] API 주소({clean_desc})에 타겟 QR({file_qr})이 없습니다! 엉뚱한 주소일 위험이 있어 버립니다.")
                    is_time_paradox = True
                else:
                    if file_dt:
                        folder_date_match = re.search(r'(\d{6})_', clean_desc)
                        if folder_date_match:
                            try:
                                folder_dt = datetime.strptime(folder_date_match.group(1), "%y%m%d")
                                diff_days = abs((file_dt - folder_dt).days)

                                # 🚨 [시간 모순 15일 검사]
                                if diff_days > 15:
                                    # 🚨 [리메이크(A/S) 프리패스 검사] 바코드 일치 여부 또는 RE 키워드 검사
                                    final_barcode_chk = str(final_case.get("barcode_id", "")).strip()
                                    is_bc_match = final_barcode_chk and len(final_barcode_chk) >= 3 and (final_barcode_chk in name_for_barcode or (final_barcode_chk.isdigit() and str(int(final_barcode_chk)) in file_numbers_int))
                                    is_re_keyword = "RE" in file_name_upper or "리메이크" in file_name_upper or "AS" in file_name_upper

                                    if is_bc_match or is_re_keyword:
                                        print(f"   🔄 [리메이크 작업] {diff_days}일 지났지만, 바코드({final_barcode_chk}) 일치 기존 주소 유지")
                                    else:
                                        print(f"   👻 [경고] 재사용 유령 QR 감지 파일({file_date_match.group(1)}) vs 주소({folder_date_match.group(1)}). 폐기")
                                        is_time_paradox = True
                            except: pass

                if not is_time_paradox:
                    if file_qr:
                        idx = clean_desc.upper().find(file_qr)
                        base_target_path = clean_desc[:idx + len(file_qr)].strip()
                    else:
                        base_target_path = re.split(r'[\r\n]', clean_desc)[0].replace('*', '').strip()

                    path_options = [base_target_path]
                    if "\\\\192.168.0.100\\scan" in base_target_path:
                        path_options.append(base_target_path.replace("\\\\192.168.0.100\\scan", "Z:"))
                    if "Z:\\" in base_target_path:
                        path_options.append(base_target_path.replace("Z:\\", "\\\\192.168.0.100\\scan\\"))

                    for opt_path in path_options:
                        if os.path.exists(opt_path):
                            final_target_path = opt_path
                            break
            else:
                print("   ⚠️ API 장부에 환자는 있으나 '주소(desc)'가 빈칸입니다!")

        # =====================================================
        # 🛤️ 3단계. Track C
        # =====================================================
        if not final_target_path:
            if not file_qr:
                print(f"   🚨 매칭할 QR코드 없고, 바코드도 없음.")
            else:
                print(f"   🚨 API 경로 탐색 실패(또는 과거 주소 폐기). 찐막 [Track C - NAS 물리 수색]")
                nas_base_paths = [
                    r"\\192.168.0.100\scan\QR\26",
                    r"Z:\ROYDENT QRCODE SYSTEM\2026",
                    r"Z:\QR\26",
                    r"\\192.168.0.100\scan\ROYDENT QRCODE SYSTEM\2026"
                ]

                all_matched_nas_folders = []
                for base in nas_base_paths:
                    if os.path.exists(base):
                        try:
                            for folder in os.listdir(base):
                                if file_qr in folder.upper() and os.path.isdir(os.path.join(base, folder)):
                                    all_matched_nas_folders.append(os.path.join(base, folder))
                        except Exception:
                            pass

                if all_matched_nas_folders:
                    all_matched_nas_folders.sort(key=lambda x: os.path.basename(x), reverse=True)
                    final_target_path = all_matched_nas_folders[0]
                    print(f"   🎯 [Track C 패스] 전체 NAS "
                          f"가장 최신 폴더 '{os.path.basename(final_target_path)}'를 찾음")

        # =====================================================
        # 4th. 장부 처리 및 파일 이동
        # =====================================================
        if final_target_path:
            if file_qr and file_qr not in final_target_path.upper():
                print(f"   ❌ [치명적 에러] 최종 목적지({final_target_path})에 타겟 QR({file_qr})이 없습니다! 이동 강제 차단.")
                continue

            p_name = final_case.get('patient_name', '알수없음') if final_case else '알수없음'
            c_name = final_case.get('client_name', '알수없음') if final_case else '알수없음'
            t_info = final_case.get('tooth', '치식 없음') if final_case else '치식 없음'
            final_barcode = str(final_case.get("barcode_id", "")).strip() if final_case else ""

            needs_special_log = False
            logged_types = set()

            if all_matched_cases:
                for c in all_matched_cases:
                    ctype = str(c.get('type_name', ''))
                    if "CUSTOM" in ctype.upper() or "PMMA" in ctype.upper():
                        needs_special_log = True
                        logged_types.add(ctype)
            elif final_case:
                ctype = str(final_case.get('type_name', ''))
                if "CUSTOM" in ctype.upper() or "PMMA" in ctype.upper():
                    needs_special_log = True
                    logged_types.add(ctype)

            try:
                dest_path = os.path.join(final_target_path, item_name)
                actual_saved_name = item_name

                if os.path.exists(dest_path):
                    print(f"   ⚠️ 동일한 이름의 폴더/파일 존재! '_수정본'을 붙여서 이동.")

                    is_file = os.path.isfile(full_src)
                    if is_file:
                        name_only, ext = os.path.splitext(item_name)
                    else:
                        name_only, ext = item_name, ""

                    item_name_new = name_only + "_수정본" + ext
                    dest_path = os.path.join(final_target_path, item_name_new)
                    actual_saved_name = item_name_new

                    counter = 2
                    while os.path.exists(dest_path):
                        item_name_new = name_only + f"_수정본{counter}" + ext
                        dest_path = os.path.join(final_target_path, item_name_new)
                        actual_saved_name = item_name_new
                        counter += 1

                shutil.move(full_src, dest_path)
                print(f"   ✅ 완료: {final_target_path}")

                if needs_special_log:
                    t_type_combined = ", ".join(logged_types)
                    log_special_work(actual_saved_name, file_qr, final_barcode, p_name, c_name, t_info, t_type_combined, final_target_path)

            except Exception as e:
                print(f"   ❌ 파일 이동 실패: {e}")
        else:
            print(f"   💀 [최종 실패] 매칭할 수 없습니다.")

if __name__ == "__main__":
    start_safe_move_process()
    print("\n" + "="*40)
    print("모든 작업이 완료되었습니다.")
    input("닫으려면 엔터(Enter) 키를 누르세요...")