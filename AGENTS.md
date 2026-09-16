# 공통 개발 규칙

## Ponytail mode

When writing or modifying code, use Ponytail style: lazy senior developer mode.
Lazy means efficient, not careless. Prefer the smallest correct change.

Before writing code, stop at the first rung that holds:

1. Does this need to be built at all? If not, skip it.
2. Does this already exist in the codebase? Reuse existing helpers, utilities, and patterns.
3. Does the standard library already solve it? Use that.
4. Does the platform or browser already provide it? Use that.
5. Does an already-installed dependency solve it? Use that.
6. Only then, write the minimum code that works.

Rules:

- No new dependency unless clearly justified.
- No boilerplate nobody asked for.
- Prefer deletion over addition.
- Prefer boring code over clever code.
- Touch the fewest files possible.
- Fix root causes, not symptoms.
- For bug fixes, check related callers and shared functions before patching only the reported path.
- Leave one small runnable check for non-trivial logic when practical.

Do not reduce:

- understanding the problem
- reading the relevant code path
- security
- input validation at trust boundaries
- error handling that prevents data loss
- accessibility
- anything explicitly requested by the user

If a simpler solution has a known limitation, mark it with a `한계:` comment and mention the upgrade path.

## 주석

- 모든 코드 수정사항에 대해 주석을 상세히 달아줘. 단 UI변경 코드는 세세하게 주석을 달 필요는 없어.
- 한 줄짜리 주석이나 문장형으로 쓸 필요 없는 주석은 개조식으로 달아줘.
- 새로운 함수를 추가할 때는 반드시 함수 주석을 달아줘.
- 함수 주석 형태는 다른 함수를 참고해서 JSDoc 형태로 달아줘. 설명과 파라미터 주석 사이에는 한 줄 띄어줘. 파라미터가 없거나 return 이 없으면 굳이 void로 달지 않아도 돼.
- JSDoc의 타입형태 중 Array는 Array대신 string[]이나 number[]처럼 어떤 타입인지를 더 구체적으로 명시해줘. 만약 타입이 혼합돼있으면 any[]로 달아줘. 이중 배열은 string[][]과 같이 표시해줘.
- 주석을 달 때 과거 버그 원인을 설명하는 주석은 달지 말아줘. 현재 코드를 설명하는 데 중점을 둬.
- 주석 줄바꿈을 할 땐 문장단위나 쉼표 단위로 끊어서 줄바꿈을 해줘. 문장이 길면 공백 포함 최소 100자 이상은 넘어갈 때만 줄바꿈을 해줘. 지금은 너무 줄바꿈을 자주 하는 거 같아.
- 커밋메시지를 작성해달라고 하면 변경을 반복하면서 주석과 변경 내용이 달라지진 않았는지 한 번 더 점검해줘.

## 작업 원칙

- 내 의견이 맞다고만 하지 말고 내가 어떤 의견을 말해도 객관적으로 평가하고 반박할 수 있는 건 반박해줘.
- 내가 클로드 리뷰를 복사 + 붙여넣기 해서 수정해달라고 하면 타당한 건 수정하되 반박할 건 반박하고 그 이유를 알려줘. 무조건 수용하진 말아줘.
- 복잡한 개발일 경우 커밋을 나눠야 할지 먼저 판단해줘. 커밋을 나눠야 한다면 커밋 단위로 작업계획을 만들어줘.

## 커밋 규칙

- 커밋 메시지 형식:

  ```text
  feat: Add feature title

  기능 제목 (한글)
  - 상세 변경 사항 1
  - 상세 변경 사항 2
  ```

  - prefix 예: `feat`, `fix`, `chore`, `docs`, `refactor`, `test` 등
  - 변경내용이 있는 상태에서 추가 수정 요청이 들어올 경우 수정할 내용이 직전 변경내용과 별도로 커밋을 구분해야 할 내용이라면 먼저 임시로 커밋을 하는 게 어떤지 제안해줘.

- 다음과 같은 당연한 기술적인 내용은 커밋메시지에 넣지 않아도 돼.
  - try ~ catch로 예외 발생 처리
  - destroy 시 column order/width 관련 listener 정리로 리소스 누수 방지
- 커밋메시지의 상세 변경사항은 기능을 중심으로 무엇을 왜 변경했는지에 초점을 맞춰줘.
