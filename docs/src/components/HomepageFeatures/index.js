import clsx from 'clsx';
import Heading from '@theme/Heading';
import Link from '@docusaurus/Link';
import styles from './styles.module.css';

const FeatureList = [
  {
    title: 'Документация',
    Svg: require('@site/static/img/undraw_docusaurus_mountain.svg').default,
    description: (
      <>
        Общая информация о системе, архитектура и ключевые требования к сервису
        управления подписками Happ.
      </>
    ),
    link: '/docs/intro',
    linkLabel: 'Открыть раздел',
  },
  {
    title: 'Архитектура',
    Svg: require('@site/static/img/undraw_docusaurus_tree.svg').default,
    description: (
      <>
        Архитектурная схема, модели данных и описание структуры решения для
        разработки и сопровождения.
      </>
    ),
    link: '/docs/architecture/arch',
    linkLabel: 'Открыть архитектуру',
  },
  {
    title: 'API',
    Svg: require('@site/static/img/undraw_docusaurus_react.svg').default,
    description: (
      <>
        Справочник по API и интерактивная OpenAPI-документация для интеграции
        с сервисом.
      </>
    ),
    link: '/docs/api-spec/api-reference',
    linkLabel: 'Открыть API',
  },
];

function Feature({Svg, title, description, link, linkLabel}) {
  return (
    <div className={clsx('col col--4')}>
      <div className="text--center">
        <Svg className={styles.featureSvg} role="img" />
      </div>
      <div className="text--center padding-horiz--md">
        <Heading as="h3">{title}</Heading>
        <p>{description}</p>
        <Link className="button button--outline button--primary button--sm" to={link}>
          {linkLabel}
        </Link>
      </div>
    </div>
  );
}

export default function HomepageFeatures() {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className="row">
          {FeatureList.map((props, idx) => (
            <Feature key={idx} {...props} />
          ))}
        </div>
      </div>
    </section>
  );
}
