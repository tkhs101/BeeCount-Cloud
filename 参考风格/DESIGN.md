---
name: Cupertino Ledger
colors:
  surface: '#fcf8fb'
  surface-dim: '#dcd9dc'
  surface-bright: '#fcf8fb'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f6f3f5'
  surface-container: '#f0edef'
  surface-container-high: '#eae7ea'
  surface-container-highest: '#e4e2e4'
  on-surface: '#1b1b1d'
  on-surface-variant: '#414753'
  inverse-surface: '#303032'
  inverse-on-surface: '#f3f0f2'
  outline: '#717785'
  outline-variant: '#c1c6d6'
  surface-tint: '#005cbb'
  primary: '#0059b5'
  on-primary: '#ffffff'
  primary-container: '#0071e3'
  on-primary-container: '#fcfbff'
  inverse-primary: '#abc7ff'
  secondary: '#006e28'
  on-secondary: '#ffffff'
  secondary-container: '#6ffb85'
  on-secondary-container: '#00732a'
  tertiary: '#ba0009'
  on-tertiary: '#ffffff'
  tertiary-container: '#e1231e'
  on-tertiary-container: '#fffaf9'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#d7e2ff'
  primary-fixed-dim: '#abc7ff'
  on-primary-fixed: '#001b3f'
  on-primary-fixed-variant: '#00458f'
  secondary-fixed: '#72fe88'
  secondary-fixed-dim: '#53e16f'
  on-secondary-fixed: '#002107'
  on-secondary-fixed-variant: '#00531c'
  tertiary-fixed: '#ffdad5'
  tertiary-fixed-dim: '#ffb4aa'
  on-tertiary-fixed: '#410001'
  on-tertiary-fixed-variant: '#930005'
  background: '#fcf8fb'
  on-background: '#1b1b1d'
  surface-variant: '#e4e2e4'
typography:
  display-lg:
    fontFamily: Inter
    fontSize: 32px
    fontWeight: '700'
    lineHeight: 40px
  headline-lg:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
  headline-md:
    fontFamily: Inter
    fontSize: 20px
    fontWeight: '600'
    lineHeight: 28px
  headline-sm:
    fontFamily: Inter
    fontSize: 17px
    fontWeight: '600'
    lineHeight: 24px
  body-lg:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: '400'
    lineHeight: 22px
  body-md:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 18px
  body-sm:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 16px
  label-md:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '600'
    lineHeight: 16px
  label-sm:
    fontFamily: Inter
    fontSize: 11px
    fontWeight: '500'
    lineHeight: 14px
  numeric-metric:
    fontFamily: Inter
    fontSize: 22px
    fontWeight: '600'
    lineHeight: 28px
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  gutter: 1rem
  margin: 1.25rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 0.875rem
  space-lg: 1.25rem
  space-xl: 1.75rem
---

## Brand & Style

The design system embodies the minimalist, refined, and calm aesthetic of modern macOS software. Built for deliberate, mindful personal financial tracking, it emphasizes clarity, precision, and emotional composure over clutter and high-stimulus visuals. The interface brings the precision of Apple’s Human Interface Guidelines into a contemporary web context: rounded containers, quiet layered glass surfaces, legible typography, and meaningful semantic color indicators.

The design movement combines **Minimalism** and subtle **Glassmorphism (Frosted Surface Depth)**. Pure canvas neutral tones (`#F5F5F7`) host clean white frosted cards (`#FFFFFF` with subtle opacity and backdrop filtering), hairline inner borders, and diffused ambient drop shadows. Visual noise is stripped away so the user can digest transactions, asset trends, and categorization at a glance.

## Colors

The palette is tuned around Apple's iconic semantic color system:
- **Primary (`#0071E3`)**: Cupertino blue used for interactive targets, selected navigation states, primary buttons, and link items.
- **Secondary (`#34C759`)**: Apple green designating financial inflows, positive cash flows, savings progress, and asset increases.
- **Tertiary (`#FF3B30`)**: Signal red reserved strictly for expenses, budget breaches, negative adjustments, and urgent actions.
- **Accent / Warnings (`#FF9500`)**: Warm amber for net assets, pending transactions, or active filters.
- **Backgrounds**: The canvas sits on a unified neutral gray base (`#F5F5F7`), while card surfaces utilize pure `#FFFFFF` with translucent variants (`rgba(255, 255, 255, 0.82)`) over backdrop-blur for elevated sheets and toolbars.
- **Text & Borders**: Primary text uses deep titanium `#1D1D1F`, secondary text drops to `#86868B`, and container borders use hairline neutral borders (`rgba(0, 0, 0, 0.06)`).

## Typography

The typographic hierarchy prioritizes systematic clarity, leveraging **Inter** as the digital equivalent to SF Pro for its clean neutral grotesque anatomy and exceptional tabular numeric legibility. 

Financial figures (`numeric-metric`, currency tags) should always use tabular numeric OpenType features (`tnum`) to keep decimal alignments stable across columns and rapid data updates. Headlines maintain tight letter-spacing (`-0.015em` to `-0.022em`) to mirror native macOS title bars, while micro-labels and table headings lean into uppercase or semi-bold optical clarity with neutral secondary contrast.

## Layout & Spacing

The layout structure mirrors a native macOS desktop utility application:
- **Left Sidebar Navigation**: Fixed compact utility rail (68px–76px on desktop) housing stacked rounded icon triggers (Book, Assets, Saving, Analysis, Setup) and account avatars.
- **Top Header Bar**: Streamlined 52px status header displaying window controls (traffic light dots `#FF5F56`, `#FFBD2E`, `#27C93F`), view title, date range switchers, and inline search pills.
- **Content Dashboard**: A modular multi-column dashboard grid. The main book view deploys a 4-card metric summary row, followed by a dynamic dual/triple column division for analytics charts (pie distribution and daily bar metrics) paired with the interactive calendar matrix and chronological transaction ledger.
- **Responsive Adaptation**: On mobile/tablet, the sidebar collapses into a floating bottom pill bar or hidden sheet, and grid columns stack vertically with outer section margins reducing from `1.25rem` to `1rem`.

## Elevation & Depth

Visual depth avoids harsh contrast or heavy drops, employing soft, optical Apple-grade layering:
- **Base Canvas**: Solid `#F5F5F7`.
- **Level 1 (Cards & Modules)**: Pure white background `#FFFFFF` or translucent `rgba(255, 255, 255, 0.88)` with `backdrop-filter: blur(20px)`, framed with an ultra-fine border `1px solid rgba(0, 0, 0, 0.05)`. Shadow: `box-shadow: 0 4px 20px rgba(0, 0, 0, 0.035), 0 1px 2px rgba(0, 0, 0, 0.02)`.
- **Level 2 (Popovers, Tooltips & Active Dragged Elements)**: Elevated frosted layers with `box-shadow: 0 12px 32px rgba(0, 0, 0, 0.08), 0 2px 6px rgba(0, 0, 0, 0.04)`.
- **Interactive State Elevation**: Buttons and pills avoid lift animations; they express states through background lightness shifts (`rgba(0, 0, 0, 0.03)` on hover, `scale(0.98)` on press).

## Shapes

The design uses continuous corner smoothing inspired by Apple's squircle geometry:
- **Main Dashboard Cards**: Large smooth corners using `16px` to `20px` (`rounded-2xl` equivalent).
- **Navigation Items & Buttons**: Moderately rounded items (`10px` to `12px` / `rounded-xl`), creating compact, comfortable touch targets.
- **Pills & Status Tags**: Fully rounded (`9999px`) for segmented switchers (e.g., `Exp / Inc / Bal`), category badges, and user status indicators.

## Components

### Buttons & Segmented Controls
- **Primary Buttons**: Vibrant `#0071E3` fill with white text, `border-radius: 10px`, height `36px`, subtle hover brightness adjustment.
- **Segmented Controls (Exp / Inc / Bal)**: Contained inside a recessed `#EFEFF1` track with a sliding `#FFFFFF` pill thumb with `box-shadow: 0 2px 6px rgba(0, 0, 0, 0.08)`.

### Cards & Metric Tiles
- Header badge displays an icon inside a soft tinted circular background (e.g., `#FF3B30` at 12% opacity with red icon).
- Top value rendered in large bold tabular type (`22px`), followed by secondary muted comparisons (`Income vs Last Month`).

### Calendar View & Activity Grids
- Clean monthly grid with muted day labels (`Sun` through `Sat`).
- Day cells feature dual line micro-metrics: red negative balance for expense, green positive balance for income, with circular active selection ring around the current date.

### List Items & Transaction Streams
- Left item displays rounded category avatar (e.g., Food, Coffee, Transit) with custom monochrome iconography.
- Center displays title, note, and timestamp in muted subtitle format.
- Right displays formatted currency: `-¥9.90` (expense red) or `+¥200.00` (income green) with secondary bank / wallet tag (`Alipay`, `Debit Card`).

### Data Visualizations
- **Donut Chart**: Fine stroke ring with center total expenditure callout and floating category tags.
- **Bar Chart**: Narrow vertical bar columns with rounded caps (`4px`), featuring interactive vertical cursor hover states showing single-day aggregate popups.