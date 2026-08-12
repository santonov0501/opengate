import Link from '@docusaurus/Link';
import styles from './styles.module.css';

const FeatureList = [
  {
    title: 'Документация',
    description: 'Общая информация о системе',
    link: '/docs/intro',
  },
  {
    title: 'Архитектура',
    description: 'Схемы и модели данных',
    link: '/docs/architecture/arch',
  },
  {
    title: 'API',
    description: 'Справочник по API',
    link: '/docs/api-spec/api-reference',
  },
];

function Feature({title, description, link}) {
  return (
    <div className={styles.feature}>
      <Link to={link} className={styles.featureLink}>
        <h3>{title}</h3>
        <p>{description}</p>
      </Link>
    </div>
  );
}

export default function HomepageFeatures() {
  return (
    <section className={styles.features}>
      <div className="container">
        {FeatureList.map((props, idx) => (
          <Feature key={idx} {...props} />
        ))}
      </div>
    </section>
  );
}
