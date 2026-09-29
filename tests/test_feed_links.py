from __future__ import annotations

import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from daily_us.config import SiteConfig
from daily_us.site import UsInsightClient


def _card(href: str, kind: str, category: str, title: str) -> str:
    """실제 피드 카드처럼 작성자·[글 유형, 카테고리] 버튼·제목 순서로 구성한 카드 HTML 생성.

    Args:
        href: 게시글 주소.
        kind: 글 유형 라벨.
        category: 카테고리 라벨.
        title: 게시글 제목.

    Returns:
        카드 하나의 HTML.
    """
    return (
        f'<a href="{href}"><div><div>서재형의 투자학교 1일 전</div>'
        # 실제 카드처럼 flex 버튼으로 두어 innerText에서 두 라벨 사이 공백 유지
        f'<div><button style="display:flex"><span>{kind}</span><span>{category}</span></button>'
        f"<div><p>{title}</p></div></div><p>123</p><span>읽음</span></div></a>"
    )


FEED_HTML = "".join([
    '<nav><a href="/club/13">기업분석도감</a></nav>',
    _card("/secrets/1", "글", "학급반장에게 무엇이든 물어보세요", "[학급반장 통신문] 가을학기 기업분석도감이 조금 더 든든해집니다."),
    _card("/secrets/2", "글", "언제나 데이트", "🙋 6월 20일 담쌤의 언제나 데이트ㅣ기업분석도감 작업을 마치고 급우님들 찾아뵙습니다"),
    _card("/secrets/3", "글", "투자학교 가을학기 기업분석도감", "[기업분석도감] 가을학기 다섯번째 기업분석도감이 도착했습니다!"),
    _card("/secrets/4", "영상", "언제나 데이트", "마음까지 넉넉해지는 한가위"),
])


class FeedLinkCategoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        """피드 추출 스크립트를 실제 브라우저에서 실행하도록 로컬 HTML 페이지 준비."""
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.page = cls.browser.new_page()
        cls.page.set_content(FEED_HTML)
        # 추출 메서드는 설정을 쓰지 않으므로 브라우저를 띄우지 않은 클라이언트 사용
        cls.client = UsInsightClient(SiteConfig("https://example.test/feed", Path("p"), Path("a"), Path("s"), True, 1000))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def test_matches_category_label_only(self) -> None:
        """제목에 키워드가 있어도 카테고리가 다른 글과 메뉴 링크는 제외하고, 제목은 카드 전체 텍스트로 유지."""
        with self.assertLogs("daily_us.site", level="WARNING") as logs:
            posts = self.client._extract_post_links(self.page, "기업분석도감", 5)

        self.assertEqual([post.url for post in posts], ["/secrets/3"])
        self.assertIn("글 투자학교 가을학기 기업분석도감 [기업분석도감]", posts[0].title)
        self.assertIn("Skipped 1 feed link(s)", logs.output[0])

    def test_type_label_stays_available_for_exclusion(self) -> None:
        """같은 카테고리의 영상 글도 후보로 찾고, 제외 설정이 쓰는 글 유형 라벨을 제목에 남김."""
        posts = self.client._extract_post_links(self.page, "언제나 데이트", 5)

        self.assertEqual([post.url for post in posts], ["/secrets/2", "/secrets/4"])
        self.assertIn("영상 언제나 데이트", posts[1].title)

    def test_empty_keyword_returns_only_post_cards(self) -> None:
        """키워드가 없으면 카테고리가 있는 게시글 카드만 한도까지 수집."""
        posts = self.client._extract_post_links(self.page, "", 3)

        self.assertEqual([post.url for post in posts], ["/secrets/1", "/secrets/2", "/secrets/3"])


if __name__ == "__main__":
    unittest.main()
